"""Test du registre d'avions par immatriculation."""

from __future__ import annotations

import pytest

from aerobriefer.aircraft.registry import DEFAULT_REGISTRATION, resolve


def test_resolve_par_immatriculation():
    ac = resolve("F-ZZZZ")
    assert "F-ZZZZ" in ac.name


def test_defaut_est_l_exemple():
    assert resolve(None).name == resolve(DEFAULT_REGISTRATION).name


def test_alias_modele_tolere():
    assert resolve("DR400").name == resolve("F-ZZZZ").name


def test_immat_inconnue_leve():
    with pytest.raises(KeyError):
        resolve("F-XXXX")
