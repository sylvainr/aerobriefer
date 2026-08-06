"""Test du rendu HTML de la checklist (pur, sans Chrome/PDF)."""

from __future__ import annotations

from aerobriefer.render.checklist import render_checklist_html


def test_checklist_contient_entete_et_sections():
    html = render_checklist_html(
        origin="LFCY",
        destination="LFBD",
        date_label="07/08/2026",
        aircraft_name="DR400/160 (F-ZZZZ)",
        sunset="21:18",
        night="21:48",
        fuel_min_l="40",
        vac_icaos=["LFCY", "LFBD"],
    )
    assert "LFCY" in html and "LFBD" in html
    assert "21:18" in html and "21:48" in html
    assert "40" in html
    # Les grandes sections de la prépa (casse du gabarit ; l'affichage MAJUSCULE
    # vient du CSS text-transform).
    for section in ("Météo", "NOTAM", "performances", "Navigation", "sécurité"):
        assert section in html
    assert "☐" in html  # cases à cocher (CSS ::before)
