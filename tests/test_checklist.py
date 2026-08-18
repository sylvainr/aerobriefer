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
    # Les étapes de la méthode « entonnoir » (0→8), ordre terrains → … → route.
    for section in (
        "cadre du vol",
        "terrains",
        "espaces aériens",
        "météo",
        "route",
        "carburant",
        "centrage",
        "log de nav",
        "Décision",
    ):
        assert section in html
    # Principe : la route (étape 4) est une CONCLUSION — elle vient APRÈS les
    # terrains (étape 1) et la météo (étape 3).
    assert html.index("1 ·") < html.index("4 ·")
    assert html.index("3 ·") < html.index("4 ·")
    # Cases à cocher cliquables : le JS injecte une vraie checkbox et barre l'item.
    assert "cb.type = 'checkbox'" in html
    assert "line-through" in html
