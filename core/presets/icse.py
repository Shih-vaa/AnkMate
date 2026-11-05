"""ICSE preset: the starting academic structure for an ICSE-affiliated school.

This is only DATA that gets loaded into the database. A CBSE or government-school preset is
another file of the same shape. After loading, the school can edit everything in the admin.

NOTE: the class list, subject lists and ALL fee amounts below are placeholders for the demo.
Verify with the school before real use (their own class range, subjects offered, and fees).
"""
from decimal import Decimal

PRESET = {
    "key": "icse",
    "board": "ICSE",
    # Indian academic session: 1 April to 31 March.
    "year_start_month": 4,
    "year_start_day": 1,
    "sections": ["A", "B"],
    # (name, order, stage)
    "grades": [
        ("Nursery", 1, "Pre-primary"),
        ("LKG", 2, "Pre-primary"),
        ("UKG", 3, "Pre-primary"),
        ("Class I", 4, "Primary"),
        ("Class II", 5, "Primary"),
        ("Class III", 6, "Primary"),
        ("Class IV", 7, "Primary"),
        ("Class V", 8, "Primary"),
        ("Class VI", 9, "Middle"),
        ("Class VII", 10, "Middle"),
        ("Class VIII", 11, "Middle"),
        ("Class IX", 12, "Secondary"),
        ("Class X", 13, "Secondary"),
    ],
    # (code, name)
    "subjects": [
        ("ENG", "English"),
        ("ENG-LANG", "English Language"),
        ("ENG-LIT", "English Literature"),
        ("HIN", "Hindi"),
        ("MATH", "Mathematics"),
        ("EVS", "Environmental Studies"),
        ("GK", "General Knowledge"),
        ("ART", "Art & Craft"),
        ("RHY", "Rhymes & Music"),
        ("COMP", "Computer Studies"),
        ("PE", "Physical Education"),
        ("MORAL", "Moral Science"),
        ("PHY", "Physics"),
        ("CHE", "Chemistry"),
        ("BIO", "Biology"),
        ("HC", "History & Civics"),
        ("GEO", "Geography"),
        ("CA", "Computer Applications"),
    ],
    # subjects taught, by stage
    "stage_subjects": {
        "Pre-primary": ["ENG", "HIN", "MATH", "EVS", "ART", "RHY"],
        "Primary": ["ENG-LANG", "ENG-LIT", "HIN", "MATH", "EVS", "GK", "COMP", "ART", "PE", "MORAL"],
        "Middle": ["ENG-LANG", "ENG-LIT", "HIN", "MATH", "PHY", "CHE", "BIO", "HC", "GEO", "COMP", "ART", "PE", "MORAL"],
        "Secondary": ["ENG-LANG", "ENG-LIT", "HIN", "MATH", "PHY", "CHE", "BIO", "HC", "GEO", "CA", "PE"],
    },
    # (code, name)
    "fee_heads": [
        ("ADM", "Admission Fee"),
        ("ANN", "Annual Charges"),
        ("TUI", "Tuition Fee"),
        ("COMP", "Computer Fee"),
        ("EXAM", "Examination Fee"),
    ],
    # DUMMY amounts in rupees: stage -> [(head code, amount, frequency)]
    "fees_by_stage": {
        "Pre-primary": [("ADM", Decimal("10000"), "one_time"), ("ANN", Decimal("5000"), "annual"), ("TUI", Decimal("1800"), "monthly")],
        "Primary": [
            ("ADM", Decimal("10000"), "one_time"), ("ANN", Decimal("5500"), "annual"), ("TUI", Decimal("2200"), "monthly"),
            ("COMP", Decimal("300"), "monthly"), ("EXAM", Decimal("600"), "half_yearly"),
        ],
        "Middle": [
            ("ADM", Decimal("12000"), "one_time"), ("ANN", Decimal("6000"), "annual"), ("TUI", Decimal("2600"), "monthly"),
            ("COMP", Decimal("400"), "monthly"), ("EXAM", Decimal("700"), "half_yearly"),
        ],
        "Secondary": [
            ("ADM", Decimal("12000"), "one_time"), ("ANN", Decimal("7000"), "annual"), ("TUI", Decimal("3200"), "monthly"),
            ("COMP", Decimal("500"), "monthly"), ("EXAM", Decimal("900"), "half_yearly"),
        ],
    },
}
