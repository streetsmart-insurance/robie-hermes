"""Carrier pull QA Drive layout (decided 2026-10-08): top-level per carrier.

    Robie Carrier Pull QA (Nicole)/<Carrier>/<YYYY-MM-DD>/<file>.pdf

The root and each carrier folder already exist (owner carlo@). An accidental
nested copy "Robie Carrier Pull QA (Nicole)/Robie Carrier Pull QA (Nicole)/..."
(created 2026-10-07, id 1_gpeEPibWrHoCuWk-7YxEuKTu9kKvn_q) is retired: never
write there. Upload from the pull workers is still not wired (every
``--upload-drive`` flag fails closed); this map is the single source of truth
for where QA packs and proof PDFs go.
"""

from __future__ import annotations

CARRIER_QA_DRIVE_ROOT_ID = "1cLEpR-0T6KdiVjcdAr0qpGTO447MetI2"
CARRIER_QA_DRIVE_ROOT_NAME = "Robie Carrier Pull QA (Nicole)"
RETIRED_NESTED_ROOT_ID = "1_gpeEPibWrHoCuWk-7YxEuKTu9kKvn_q"

# carrier key (as in carrier_dry_run.SPECS) -> (folder title, folder id)
CARRIER_QA_FOLDERS: dict[str, tuple[str, str]] = {
    "progressive": ("Progressive", "1MMojqm99ft4DgxplMuBvz-eTnKdgpY9U"),
    "progressive_bop": ("Progressive BOP", "122J0nQ85jW26eEyRReUCqyMOpLiAHuHo"),
    "guard": ("Guard", "13NlvNvTm5Urki88xkFhFuCGdnIGIWCjX"),
    "geico": ("Geico", "1mMy9nrYjN8PRRwihRLgjjDdb213WqBLt"),
    "travelers": ("Travelers", "1dAHrhxn_ksrbxqAYAxpOWjtbhjuVO39t"),
    "natgen": ("NatGen", "1jpyLNJmuRvhZ-EYnM2sGQRCXfyQjsHh9"),
    "uticafirst": ("Utica First", "1fn7q_tBK19Z-PqOs7McYzpdiaBcAogBj"),
    "farmersofsalem": ("Farmers of Salem", "1oz9aYLVxre7UMzzgG3rRe_GqVS4tZn-G"),
}


def qa_folder_path(carrier: str, day_iso: str) -> str:
    """Human path for one carrier's dated QA folder, e.g. for READMEs."""
    title, _ = CARRIER_QA_FOLDERS[carrier]
    return f"{CARRIER_QA_DRIVE_ROOT_NAME}/{title}/{day_iso}"
