from datetime import datetime, timedelta
import ast
import json
from pathlib import Path
import random
import re
import site
import subprocess
import sys
import textwrap
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parent.parent
VENV_SITE_PACKAGES = PROJECT_ROOT / ".venv" / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
if VENV_SITE_PACKAGES.exists():
    site.addsitedir(VENV_SITE_PACKAGES)

from flask import Flask, redirect, render_template, request, session, url_for

from problems import MISSIONS, PROBLEMS


app = Flask(__name__)
app.secret_key = "coding-quiz-exhibition-secret"

RANKINGS = []
MAX_RANKINGS = 100
QUIZ_TIME_LIMIT_SECONDS = 30 * 60
MAX_HINTS = 3
QUIZ_MAX_SCORE = sum(problem["score"] for problem in PROBLEMS)
MISSION_MAX_SCORE = sum(mission["score"] for mission in MISSIONS.values())
TOTAL_MAX_SCORE = QUIZ_MAX_SCORE + MISSION_MAX_SCORE
RAW_QUIZ_MAX_SCORE = sum(problem["score"] for problem in PROBLEMS)
DANGEROUS_KEYWORDS = ["open", "exec", "eval", "os", "sys", "subprocess", "socket"]
ALLOWED_IMPORT_LINES = {
    "from flask import request",
    "from uuid import uuid4",
}
SAFE_CODE_TIMEOUT_SECONDS = 2


def reset_progress(nickname):
    session.clear()
    session["nickname"] = nickname
    session["quiz_index"] = 0
    session["quiz_score"] = 0
    session["lv3_correct_count"] = 0
    session["answered_problem_ids"] = []
    session["answers"] = {}
    session["correct_problem_ids"] = []
    session["hinted_problem_ids"] = []
    session["hint_used_count"] = 0
    session["quiz_started_at"] = datetime.now().isoformat()
    session["quiz_submitted"] = False
    session["develop_score"] = 0
    session["selected_mission"] = ""
    session["develop_answers"] = {}
    session["develop_scores"] = {}
    session["develop_passed_ids"] = []
    session["result_id"] = ""


def get_quiz_started_at():
    raw_started_at = session.get("quiz_started_at")
    if not raw_started_at:
        started_at = datetime.now()
        session["quiz_started_at"] = started_at.isoformat()
        session.modified = True
        return started_at
    return datetime.fromisoformat(raw_started_at)


def get_remaining_seconds():
    ends_at = get_quiz_started_at() + timedelta(seconds=QUIZ_TIME_LIMIT_SECONDS)
    return max(0, int((ends_at - datetime.now()).total_seconds()))


def is_quiz_time_up():
    return get_remaining_seconds() <= 0


def recalculate_quiz_score():
    correct_ids = set(session.get("correct_problem_ids", []))
    raw_quiz_score = sum(problem["score"] for problem in PROBLEMS if problem["id"] in correct_ids)
    lv3_correct_count = sum(1 for problem in PROBLEMS if problem["level"] == "LV3" and problem["id"] in correct_ids)
    session["raw_quiz_score"] = raw_quiz_score
    session["quiz_score"] = raw_quiz_score
    session["lv3_correct_count"] = lv3_correct_count
    session.modified = True


def recalculate_develop_score():
    scores = session.get("develop_scores", {})
    session["develop_score"] = sum(int(scores.get(mission_id, 0)) for mission_id in MISSIONS)
    session["selected_mission"] = ",".join(session.get("develop_passed_ids", [])) or "-"
    session.modified = True


def submit_quiz():
    session["quiz_submitted"] = True
    session["quiz_index"] = len(PROBLEMS)
    session.modified = True


def get_problem_index():
    raw_index = request.values.get("index", session.get("quiz_index", 0))
    try:
        index = int(raw_index)
    except (TypeError, ValueError):
        index = 0
    return min(max(index, 0), len(PROBLEMS) - 1)


def normalize_tokens(value):
    cleaned = value.strip().lower().replace(",", " ")
    return [part for part in cleaned.split() if part]


def grade_short_answer(problem, answer):
    expected = normalize_tokens(problem["answer"])
    actual = normalize_tokens(answer)
    if len(expected) > 1:
        return actual == expected
    return "".join(actual) == "".join(expected)


def has_dangerous_keyword(answer):
    lowered = answer.lower()
    if "__" in lowered:
        return True
    if re.search(r"^\s*(import|from)\s+", lowered, re.MULTILINE):
        return True
    return any(re.search(rf"\b{re.escape(keyword)}\b", lowered) for keyword in DANGEROUS_KEYWORDS)


def get_assigned_names(node):
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, (ast.Tuple, ast.List)):
        names = set()
        for item in node.elts:
            names.update(get_assigned_names(item))
        return names
    return set()


def sanitize_user_code(answer, protected_names=None):
    protected_names = set(protected_names or [])
    try:
        tree = ast.parse(answer)
    except SyntaxError:
        return None

    filtered_body = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            source = ast.get_source_segment(answer, node) or ""
            if source.strip().lower() in ALLOWED_IMPORT_LINES:
                continue
            return None

        assigned_names = set()
        if isinstance(node, ast.Assign):
            for target in node.targets:
                assigned_names.update(get_assigned_names(target))
        elif isinstance(node, ast.AnnAssign):
            assigned_names.update(get_assigned_names(node.target))
        elif isinstance(node, ast.AugAssign):
            assigned_names.update(get_assigned_names(node.target))

        if assigned_names and assigned_names.issubset(protected_names):
            continue

        filtered_body.append(node)

    tree.body = filtered_body
    sanitized = ast.unparse(tree)
    if has_dangerous_keyword(sanitized):
        return None
    return sanitized


def run_hidden_case(answer, setup_code, assert_code, expected_stdout=None):
    runner = r"""
import contextlib
import io
import json

payload = json.loads(input())
allowed_builtins = {
    "abs": abs,
    "all": all,
    "any": any,
    "bool": bool,
    "dict": dict,
    "enumerate": enumerate,
    "float": float,
    "int": int,
    "isinstance": isinstance,
    "len": len,
    "list": list,
    "max": max,
    "min": min,
    "print": print,
    "range": range,
    "round": round,
    "str": str,
    "sum": sum,
    "tuple": tuple,
}
env = {"__builtins__": allowed_builtins}

class FakeForm(dict):
    def get(self, key, default=None):
        return super().get(key, default)

class FakeRequest:
    def __init__(self, form):
        self.form = FakeForm(form)

class FakeUUID:
    hex = "hidden-test-id"

def uuid4():
    return FakeUUID()

env["FakeRequest"] = FakeRequest
env["uuid4"] = uuid4

stdout = io.StringIO()
exec(payload["setup"], env)
with contextlib.redirect_stdout(stdout):
    exec(payload["answer"], env)
env["_stdout"] = stdout.getvalue()
exec(payload["assert_code"], env)
if payload["expected_stdout"] is not None:
    assert env["_stdout"].strip() == payload["expected_stdout"].strip()
"""
    payload = {
        "answer": answer,
        "setup": setup_code,
        "assert_code": assert_code,
        "expected_stdout": expected_stdout,
    }
    try:
        completed = subprocess.run(
            [sys.executable, "-I", "-c", runner],
            input=json.dumps(payload, ensure_ascii=False) + "\n",
            text=True,
            capture_output=True,
            timeout=SAFE_CODE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def run_hidden_cases(answer, cases, protected_names=None):
    sanitized_answer = sanitize_user_code(answer, protected_names)
    if sanitized_answer is None:
        return False
    return all(run_hidden_case(sanitized_answer, **case) for case in cases)


def code_case(setup_code, assert_code, expected_stdout=None):
    return {
        "setup_code": textwrap.dedent(setup_code).strip(),
        "assert_code": textwrap.dedent(assert_code).strip(),
        "expected_stdout": expected_stdout,
    }


LV3_TEST_CASES = {
    "lv3_1": [
        code_case(
            """
            request = FakeRequest({"mbti1": "INFP", "mbti2": "ENFJ", "expected_score": "90", "nickname": "하람"})
            """,
            """
            assert mbti1 == "INFP"
            assert mbti2 == "ENFJ"
            assert expected_score == "90"
            assert nickname == "하람"
            """,
        ),
        code_case(
            """
            request = FakeRequest({"mbti1": "INTJ", "mbti2": "ENFP"})
            """,
            """
            assert mbti1 == "INTJ"
            assert mbti2 == "ENFP"
            assert expected_score == ""
            assert nickname == ""
            """,
        ),
        code_case(
            """
            request = FakeRequest({"mbti1": "ENTP", "mbti2": "ISFJ", "expected_score": "", "nickname": ""})
            """,
            """
            assert mbti1 == "ENTP"
            assert mbti2 == "ISFJ"
            assert expected_score == ""
            assert nickname == ""
            """,
        ),
    ],
    "lv3_2": [
        code_case(
            """
            COMPAT = {"INFP": {"ENFJ": 95, "ESTJ": 60}, "INTJ": {"ENFP": 90, "ESFP": 55}}
            RELATIONSHIP = {"INFP": {"ENFJ": 90, "ESTJ": 65}, "INTJ": {"ENFP": 85, "ESFP": 50}}
            CONFLICT = {"INFP": {"ENFJ": 80, "ESTJ": 45}, "INTJ": {"ENFP": 75, "ESFP": 40}}
            EXPRESSION = {"INFP": {"ENFJ": 85, "ESTJ": 55}, "INTJ": {"ENFP": 80, "ESFP": 45}}
            mbti1 = "INFP"
            mbti2 = "ENFJ"
            """,
            """
            assert (general, relationship, conflict, expression) == (95, 90, 80, 85)
            """,
        ),
        code_case(
            """
            COMPAT = {"INFP": {"ENFJ": 95}, "INTJ": {"ENFP": 90}}
            RELATIONSHIP = {"INFP": {"ENFJ": 90}, "INTJ": {"ENFP": 85}}
            CONFLICT = {"INFP": {"ENFJ": 80}, "INTJ": {"ENFP": 75}}
            EXPRESSION = {"INFP": {"ENFJ": 85}, "INTJ": {"ENFP": 80}}
            mbti1 = "INTJ"
            mbti2 = "ENFP"
            """,
            """
            assert (general, relationship, conflict, expression) == (90, 85, 75, 80)
            """,
        ),
        code_case(
            """
            COMPAT = {"INFP": {"ENFJ": 95}}
            RELATIONSHIP = {"INFP": {"ENFJ": 90}}
            CONFLICT = {"INFP": {"ENFJ": 80}}
            EXPRESSION = {"INFP": {"ENFJ": 85}}
            mbti1 = "INFP"
            mbti2 = "ISTP"
            """,
            """
            assert (general, relationship, conflict, expression) == (50, 50, 50, 50)
            """,
        ),
    ],
    "lv3_3": [
        code_case(
            """
            general = 95
            relationship = 90
            conflict = 80
            expression = 85
            def format_score(score):
                return f"{score:.1f}"
            """,
            """
            assert final_score == 89
            assert score == final_score
            assert score_display == "89.0"
            """,
        ),
        code_case(
            """
            general = 70
            relationship = 60
            conflict = 50
            expression = 40
            def format_score(score):
                return f"{score:.1f}"
            """,
            """
            assert final_score == 58
            assert score == 58
            assert score_display == "58.0"
            """,
        ),
        code_case(
            """
            general = 88
            relationship = 77
            conflict = 66
            expression = 55
            def format_score(score):
                return f"{score:.1f}"
            """,
            """
            assert final_score == 74.8
            assert score_display == "74.8"
            """,
        ),
    ],
    "lv3_4": [
        code_case('score = 95', 'assert stage_emoji == "❤️"\nassert stage_label == "운명 궁합"\nassert isinstance(stage_description, str) and stage_description'),
        code_case('score = 86', 'assert stage_emoji == "😊"\nassert stage_label == "찰떡 궁합"\nassert isinstance(stage_description, str) and stage_description'),
        code_case('score = 73', 'assert stage_emoji == "🙂"\nassert stage_label == "좋은 궁합"\nassert isinstance(stage_description, str) and stage_description'),
        code_case('score = 35', 'assert stage_emoji == "😅"\nassert stage_label == "유의 궁합"\nassert isinstance(stage_description, str) and stage_description'),
        code_case('score = 5', 'assert stage_emoji == "💔"\nassert stage_label == "극과 극 궁합"\nassert isinstance(stage_description, str) and stage_description'),
    ],
    "lv3_5": [
        code_case(
            """
            nickname = "하람"
            expected_score_float = 88.0
            score = 86.0
            difference = 2.0
            general = 90
            relationship = 85
            conflict = 80
            expression = 85
            rankings = []
            MAX_RANKINGS = 100
            """,
            """
            assert len(rankings) == 1
            assert rankings[0]["id"] == "hidden-test-id"
            assert rankings[0]["nickname"] == "하람"
            assert rankings[0]["expected_score"] == 88.0
            assert rankings[0]["final_score"] == 86.0
            assert rankings[0]["difference"] == 2.0
            assert rankings[0]["general"] == 90
            assert rankings[0]["relationship"] == 85
            assert rankings[0]["conflict"] == 80
            assert rankings[0]["expression"] == 85
            """,
        ),
        code_case(
            """
            nickname = "도윤"
            expected_score_float = 70.0
            score = 72.5
            difference = 2.5
            general = 75
            relationship = 70
            conflict = 65
            expression = 80
            rankings = [{"id": "old", "nickname": "기존"}]
            MAX_RANKINGS = 100
            """,
            """
            assert len(rankings) == 2
            assert rankings[0]["nickname"] == "도윤"
            assert rankings[1]["nickname"] == "기존"
            """,
        ),
        code_case(
            """
            nickname = "초과"
            expected_score_float = 50.0
            score = 60.0
            difference = 10.0
            general = 60
            relationship = 60
            conflict = 60
            expression = 60
            rankings = [{"id": "old1"}, {"id": "old2"}]
            MAX_RANKINGS = 2
            """,
            """
            assert len(rankings) == 2
            assert rankings[0]["nickname"] == "초과"
            assert rankings[-1]["id"] == "old1"
            """,
        ),
    ],
}


MISSION_TEST_CASES = {
    "A": [
        code_case('score = 85', 'assert "좋은 궁합" in _stdout'),
        code_case('score = 80', 'assert "좋은 궁합" in _stdout'),
        code_case('score = 79', 'assert _stdout.strip() == ""'),
    ],
    "B": [
        code_case('general = 90\nrelationship = 80\nconflict = 70\nexpression = 85', 'assert final_score == 83\nassert "83" in _stdout'),
        code_case('general = 100\nrelationship = 100\nconflict = 100\nexpression = 100', 'assert final_score == 100\nassert "100" in _stdout'),
        code_case('general = 50\nrelationship = 60\nconflict = 70\nexpression = 80', 'assert final_score == 62\nassert "62" in _stdout'),
    ],
    "C": [
        code_case(
            'general = 90\nrelationship = 80\nconflict = 70\nexpression = 85\nexpected_score = "88"',
            """
            assert final_score == 83
            assert expected_score_float == 88.0
            assert difference == 5
            assert stage_label == "찰떡 궁합"
            assert "83" in _stdout and "찰떡 궁합" in _stdout and "5" in _stdout
            """,
        ),
        code_case(
            'general = 100\nrelationship = 95\nconflict = 90\nexpression = 95\nexpected_score = "90"',
            """
            assert final_score == 96
            assert difference == 6
            assert stage_label == "운명 궁합"
            assert "운명 궁합" in _stdout
            """,
        ),
        code_case(
            'general = 70\nrelationship = 70\nconflict = 70\nexpression = 70\nexpected_score = "80"',
            """
            assert final_score == 70
            assert difference == 10
            assert stage_label == "좋은 궁합"
            assert "좋은 궁합" in _stdout
            """,
        ),
        code_case(
            'general = 55\nrelationship = 60\nconflict = 50\nexpression = 65\nexpected_score = "50"',
            """
            assert final_score == 57
            assert difference == 7
            assert stage_label == "무난한 궁합"
            assert "무난한 궁합" in _stdout
            """,
        ),
    ],
}

LV3_PROTECTED_NAMES = {
    "lv3_1": set(),
    "lv3_2": {"COMPAT", "RELATIONSHIP", "CONFLICT", "EXPRESSION", "mbti1", "mbti2"},
    "lv3_3": {"general", "relationship", "conflict", "expression"},
    "lv3_4": {"score"},
    "lv3_5": {
        "nickname",
        "expected_score_float",
        "score",
        "difference",
        "general",
        "relationship",
        "conflict",
        "expression",
        "rankings",
        "MAX_RANKINGS",
    },
}

MISSION_PROTECTED_NAMES = {
    "A": {"score"},
    "B": {"general", "relationship", "conflict", "expression"},
    "C": {"general", "relationship", "conflict", "expression", "expected_score"},
}


def grade_code_answer(problem, answer):
    cases = LV3_TEST_CASES.get(problem["id"])
    if not cases:
        return False
    return run_hidden_cases(answer, cases, LV3_PROTECTED_NAMES.get(problem["id"], set()))


def grade_problem(problem, answer):
    if problem["type"] == "multiple_choice":
        return answer.strip() == problem["answer"]
    if problem["type"] == "short_answer":
        return grade_short_answer(problem, answer)
    return grade_code_answer(problem, answer)


def grade_mission(mission_id, answer):
    cases = MISSION_TEST_CASES.get(mission_id)
    if not cases:
        return False
    return run_hidden_cases(answer, cases, MISSION_PROTECTED_NAMES.get(mission_id, set()))


def sorted_rankings():
    return sorted(
        RANKINGS,
        key=lambda entry: (
            -entry["total_score"],
            entry["elapsed_seconds"],
            entry["submitted_at"],
        ),
    )


def ranked_entries():
    ranked = []
    previous_key = None
    previous_rank = 0
    for index, entry in enumerate(sorted_rankings(), start=1):
        rank_key = (entry["total_score"], entry["elapsed_seconds"])
        rank = previous_rank if rank_key == previous_key else index
        copied = dict(entry)
        copied["rank"] = rank
        copied["elapsed_display"] = format_elapsed_seconds(entry["elapsed_seconds"])
        ranked.append(copied)
        previous_key = rank_key
        previous_rank = rank
    return ranked


def format_elapsed_seconds(total_seconds):
    minutes, seconds = divmod(int(total_seconds), 60)
    return f"{minutes:02d}:{seconds:02d}"


def get_elapsed_seconds():
    started_at = get_quiz_started_at()
    return max(0, int((datetime.now() - started_at).total_seconds()))


def seed_guest_rankings():
    if any(entry["id"].startswith("guest-") for entry in RANKINGS):
        return

    guest_totals = [95, 93, 90, 88, 81, 74, 64, 50, 33, 21]
    rng = random.Random(20260618)
    now = datetime.now()

    for index, total_score in enumerate(guest_totals, start=1):
        min_quiz_score = max(0, total_score - MISSION_MAX_SCORE)
        max_quiz_score = min(QUIZ_MAX_SCORE, total_score)
        quiz_score = rng.randint(min_quiz_score, max_quiz_score)
        develop_score = total_score - quiz_score
        elapsed_seconds = rng.randint(180, QUIZ_TIME_LIMIT_SECONDS - 1)

        RANKINGS.append(
            {
                "id": f"guest-{index:02d}",
                "nickname": f"게스트 {index:02d}",
                "total_score": total_score,
                "quiz_score": quiz_score,
                "develop_score": develop_score,
                "lv3_correct_count": rng.randint(0, 5),
                "selected_mission": "A,B,C" if develop_score else "-",
                "elapsed_seconds": elapsed_seconds,
                "submitted_at": now - timedelta(seconds=(len(guest_totals) - index + 1) * 37),
            }
        )


seed_guest_rankings()


@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        nickname = request.form.get("nickname", "").strip() or "익명 개발자"
        reset_progress(nickname)
        return redirect(url_for("quiz"))
    return render_template("index.html")


@app.route("/quiz", methods=["GET", "POST"])
def quiz():
    if "nickname" not in session:
        return redirect(url_for("index"))

    if session.get("quiz_submitted"):
        return redirect(url_for("develop"))

    quiz_index = get_problem_index()
    session["quiz_index"] = quiz_index
    session.modified = True

    problem = PROBLEMS[quiz_index]
    feedback = None
    time_up = is_quiz_time_up()

    if request.method == "POST":
        action = request.form.get("action", "submit")

        if action == "finish" or time_up:
            submit_quiz()
            return redirect(url_for("develop"))

        if action == "hint":
            hinted_ids = session.get("hinted_problem_ids", [])
            if problem["id"] not in hinted_ids and session.get("hint_used_count", 0) < MAX_HINTS:
                hinted_ids.append(problem["id"])
                session["hinted_problem_ids"] = hinted_ids
                session["hint_used_count"] = session.get("hint_used_count", 0) + 1
                session.modified = True
            return redirect(url_for("quiz", index=quiz_index))

        answer = request.form.get("answer", "")
        answers = session.get("answers", {})
        answers[problem["id"]] = answer
        session["answers"] = answers

        answered_ids = session.get("answered_problem_ids", [])
        is_correct = grade_problem(problem, answer)

        if problem["id"] not in answered_ids:
            answered_ids.append(problem["id"])
            session["answered_problem_ids"] = answered_ids

        correct_ids = session.get("correct_problem_ids", [])
        if is_correct and problem["id"] not in correct_ids:
            correct_ids.append(problem["id"])
        if not is_correct and problem["id"] in correct_ids:
            correct_ids.remove(problem["id"])
        session["correct_problem_ids"] = correct_ids
        recalculate_quiz_score()

        feedback = {
            "is_correct": is_correct,
            "answer": problem["answer"],
            "explanation": problem["explanation"],
        }

    return render_template(
        "quiz.html",
        problem=problem,
        problem_number=quiz_index + 1,
        total_problems=len(PROBLEMS),
        progress=(len(session.get("answered_problem_ids", [])) / len(PROBLEMS)) * 100,
        quiz_score=session.get("quiz_score", 0),
        quiz_max_score=QUIZ_MAX_SCORE,
        answered_ids=session.get("answered_problem_ids", []),
        correct_ids=session.get("correct_problem_ids", []),
        problems=PROBLEMS,
        saved_answer=session.get("answers", {}).get(problem["id"], ""),
        hint_is_visible=problem["id"] in session.get("hinted_problem_ids", []),
        hint_used_count=session.get("hint_used_count", 0),
        max_hints=MAX_HINTS,
        unanswered_count=len(PROBLEMS) - len(session.get("answered_problem_ids", [])),
        remaining_seconds=get_remaining_seconds(),
        time_up=time_up,
        feedback=feedback,
    )


@app.route("/develop", methods=["GET", "POST"])
def develop():
    if "nickname" not in session:
        return redirect(url_for("index"))
    if not session.get("quiz_submitted"):
        if is_quiz_time_up():
            submit_quiz()
        else:
            return redirect(url_for("quiz"))
    if not session.get("quiz_submitted"):
        return redirect(url_for("quiz"))

    selected_id = request.form.get("mission", "A") if request.method == "POST" else request.args.get("mission", "A")
    if selected_id not in MISSIONS:
        selected_id = "A"
    feedback = None

    if request.method == "POST":
        answer = request.form.get("answer", "")
        selected_id = request.form.get("mission", "A")
        if selected_id not in MISSIONS:
            selected_id = "A"
        passed = grade_mission(selected_id, answer)
        score = MISSIONS[selected_id]["score"] if passed else 0

        answers = session.get("develop_answers", {})
        scores = session.get("develop_scores", {})
        passed_ids = session.get("develop_passed_ids", [])
        answers[selected_id] = answer
        scores[selected_id] = score

        if passed and selected_id not in passed_ids:
            passed_ids.append(selected_id)
        if not passed and selected_id in passed_ids:
            passed_ids.remove(selected_id)

        session["develop_answers"] = answers
        session["develop_scores"] = scores
        session["develop_passed_ids"] = passed_ids
        recalculate_develop_score()
        session.modified = True
        feedback = {
            "mission_id": selected_id,
            "is_correct": passed,
            "score": score,
            "message": f"미션 {selected_id}를 통과했습니다!" if passed else f"미션 {selected_id}의 핵심 조건을 조금 더 채워보세요.",
        }

    return render_template(
        "develop.html",
        missions=MISSIONS,
        selected_id=selected_id,
        selected_mission=MISSIONS[selected_id],
        develop_answers=session.get("develop_answers", {}),
        develop_scores=session.get("develop_scores", {}),
        develop_passed_ids=session.get("develop_passed_ids", []),
        develop_score=session.get("develop_score", 0),
        mission_max_score=MISSION_MAX_SCORE,
        feedback=feedback,
    )


@app.route("/result", methods=["POST"])
def save_result():
    if "nickname" not in session:
        return redirect(url_for("index"))

    if not session.get("result_id"):
        result_id = uuid4().hex
        elapsed_seconds = get_elapsed_seconds()
        entry = {
            "id": result_id,
            "nickname": session.get("nickname", "익명 개발자"),
            "total_score": session.get("quiz_score", 0) + session.get("develop_score", 0),
            "quiz_score": session.get("quiz_score", 0),
            "develop_score": session.get("develop_score", 0),
            "lv3_correct_count": session.get("lv3_correct_count", 0),
            "selected_mission": session.get("selected_mission", "-"),
            "elapsed_seconds": elapsed_seconds,
            "submitted_at": datetime.now(),
        }
        RANKINGS.append(entry)
        if len(RANKINGS) > MAX_RANKINGS:
            RANKINGS.pop(0)
        session["result_id"] = result_id
        session.modified = True

    return redirect(url_for("result"))


@app.route("/result")
def result():
    if "nickname" not in session:
        return redirect(url_for("index"))

    rankings = ranked_entries()
    result_id = session.get("result_id")
    current_rank = next((item["rank"] for item in rankings if item["id"] == result_id), None)

    return render_template(
        "result.html",
        nickname=session.get("nickname", "익명 개발자"),
        quiz_score=session.get("quiz_score", 0),
        develop_score=session.get("develop_score", 0),
        total_score=session.get("quiz_score", 0) + session.get("develop_score", 0),
        quiz_max_score=QUIZ_MAX_SCORE,
        mission_max_score=MISSION_MAX_SCORE,
        total_max_score=TOTAL_MAX_SCORE,
        selected_mission=session.get("selected_mission", "-"),
        result_id=result_id,
        current_rank=current_rank,
        rankings=rankings,
    )


if __name__ == "__main__":
    app.run(debug=True, port=5001)
