"""Canned answers for QORE_FAKE=1 (local tests only, never used in production)."""
import json


def fake_reply(system, messages):
    last = messages[-1]["text"].lower() if messages else ""
    if "ONLY a JSON array" in system:
        return json.dumps([{"find": "teh", "replace": "the", "why": "spelling"}])
    if "ONLY a JSON object" in system:
        return json.dumps({"title": "ENGLISH STUDIES EXAMINATION", "subtitle": "MONTHLY EXAMINATION",
                           "class": "6", "marks": "10", "type": "exam"})
    if "MARKING SCHEME" in system and "marking scheme" in system.lower():
        return ("@title: MARKING SCHEME\n@subtitle: ENGLISH EXAM\n\nPART A: MULTIPLE CHOICE\n\n"
                "| Q | Answer | Marks |\n|---|---|---|\n| 1 | B | 2 |\n| 2 | A | 2 |\n")
    if "Return the COMPLETE updated document" in system:
        return "@title: REVISED DOCUMENT\n\n## Changed\n\nThis is the revised text.\n"
    if "Summarize" in system:
        return "The main idea is simple.\n\n- Point one\n- Point two\n"
    if "OUTPUT FORMAT" in system:
        return ("@title: ENGLISH STUDIES EXAMINATION\n@subtitle: MONTHLY EXAMINATION\n@class: 6\n@marks: 10\n\n"
                "PART A: MULTIPLE CHOICE (4 MARKS)\n\nChoose the correct answer.\n\n"
                "1. Where did the monkey live?\nA. In a jungle\nB. In a house\nC. In a boat\nD. In a car\n\n"
                "2. What did the monkey like to pluck?\nA. Rocks\nB. Fruit\nC. Sand\nD. Water\n\n"
                "PART B: SHORT ANSWERS (6 MARKS)\n\n3. Why was the crocodile sad? (3 marks)\n________________________________\n________________________________\n\n"
                "4. Write the missing word: The monkey was ________ and found a way out.\n\n"
                "| Word | Answer | Meaning |\n|---|---|---|\n| Pluck |  | To take fruit |\n| Pond |  | Small water area |\n")
    return "This is a test answer from Qore."
