from flask import Flask, request, jsonify
from flask_cors import CORS
import os
import math
import requests

app = Flask(__name__)
CORS(app)

GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-preview-09-2025:generateContent"
API_KEY = os.environ.get("GEMINI_API_KEY", "")


# -------------------------------------------------
# 1) КЛАСИФІКАЦІЯ МІСІЇ
# -------------------------------------------------
def classify_mission(time_h, radius_km):
    if time_h <= 4 and radius_km <= 50:
        return "tactical"
    if time_h <= 27 and radius_km <= 300:
        return "operational"
    return "strategic"


def choose_propulsion(mission_type, low_noise, budget):
    if mission_type == "tactical" and low_noise and budget < 7000:
        return "electric"
    if mission_type == "operational":
        return "piston_engine"
    return "turbine"


# -------------------------------------------------
# 2) ШАБЛОНИ ПЛАНЕРА
# -------------------------------------------------
TEMPLATES = {
    "tactical": {"emptyMass_kg": 2, "Cd": 0.035, "S": 0.8, "v": 20},
    "operational": {"emptyMass_kg": 50, "Cd": 0.04, "S": 8, "v": 60},
    "strategic": {"emptyMass_kg": 1500, "Cd": 0.03, "S": 40, "v": 150},
}

PROP = {
    "electric": {"eta_prop": 0.8, "eta_sys": 0.8, "battery_wh_kg": 220},
    "piston_engine": {"eta_prop": 0.8, "bsfc": 0.25},  # kg/kWh
    "turbine": {"eta_prop": 0.85, "bsfc": 0.3},
}

RHO = 1.225


# -------------------------------------------------
# 3) РОЗРАХУНКИ (з формул зі свого PDF)
# -------------------------------------------------

def aerodynamic_drag(rho, v, S, Cd):
    return 0.5 * rho * v * v * S * Cd


def cruise_power(T, v, eta):
    return T * v / eta


def electric_energy(power_W, t_h, wh_kg, eta):
    Wh = (power_W * t_h) / eta
    mass = Wh / wh_kg
    return Wh, mass


def fuel_mass(power_W, t_h, bsfc):
    return t_h * bsfc * (power_W / 1000)


def performance(v_mps, time_h):
    dist = v_mps * time_h * 3.6
    rad = dist / 2
    return dist, rad


# -------------------------------------------------
# 4) РЕКОМЕНДОВАНІ КОМПОНЕНТИ ПО БЮДЖЕТУ
# -------------------------------------------------
def component_recommendations(propulsion, budget):
    result = {}

    if propulsion == "electric":
        result["engine"] = {"model": "T-Motor U15L", "price": 1800, "reason": "Висока тяга, низький шум, оптимально для тактичних БПЛА."}
        result["propeller"] = {"model": "T-Motor 30x10 CF", "price": 250, "reason": "Легкий карбон, високий ККД."}
        result["electronics"] = {"model": "Cube Orange+ Here4 RTK", "price": 1200, "reason": "Професійна навігація для точних місій."}
    elif propulsion == "piston_engine":
        result["engine"] = {"model": "Rotax 582 UL", "price": 6500, "reason": "Відмінне співвідношення маси, ККД та вартості."}
        result["propeller"] = {"model": "E-Props 1.9m VP", "price": 2300, "reason": "Змінний крок, високий ККД."}
        result["electronics"] = {"model": "Pixhawk Cube Orange", "price": 900, "reason": "Надійний автопілот для операційних місій."}
    else:
        result["engine"] = {"model": "PBS TJ40", "price": 45000, "reason": "Легкий турбореактивний двигун для високошвидкісних платформ."}
        result["propeller"] = {"model": "N/A", "price": 0, "reason": "Реактивний двигун не потребує пропелера."}
        result["electronics"] = {"model": "Cube Orange+ ADS-B", "price": 1500, "reason": "Потрібно для безпеки високошвидкісних місій."}

    return result


# -------------------------------------------------
# 5) AI ВИСНОВОК (чіткий, технічний)
# -------------------------------------------------
def gemini_summary(mission, propulsion, mass, radius):
    if not API_KEY:
        return "AI недоступний: немає GEMINI_API_KEY"

    prompt = f"""
Ти інженер-конструктор БПЛА. Сформуй короткий (3-5 речень) технічний висновок:

- тип місії: {mission}
- силова установка: {propulsion}
- злітна маса: {mass:.1f} кг
- реальний радіус дії: {radius:.1f} км

Оціни ефективність, запас енергії, придатність до завдання.
"""

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
    }

    r = requests.post(f"{GEMINI_API_URL}?key={API_KEY}", json=payload).json()
    try:
        return r["candidates"][0]["content"]["parts"][0]["text"]
    except:
        return "AI не зміг сформувати висновок."


# -------------------------------------------------
# 6) API ENDPOINT
# -------------------------------------------------
@app.route("/api/configure", methods=["POST"])
def configure():
    d = request.get_json()

    time_h = float(d["timeHours"])
    radius_req = float(d["radiusKm"])
    payload = float(d["payloadKg"])
    lowNoise = bool(d["lowNoise"])
    budget = float(d["budget"])

    mission = classify_mission(time_h, radius_req)
    propulsion = choose_propulsion(mission, lowNoise, budget)

    air = TEMPLATES[mission]
    prop = PROP[propulsion]

    D = aerodynamic_drag(RHO, air["v"], air["S"], air["Cd"])
    T = D

    P = cruise_power(T, air["v"], prop["eta_prop"])

    if propulsion == "electric":
        Wh, energy_mass = electric_energy(P, time_h, prop["battery_wh_kg"], prop["eta_sys"])
    else:
        Wh = None
        energy_mass = fuel_mass(P, time_h, prop["bsfc"])

    MTOW = air["emptyMass_kg"] + payload + energy_mass

    range_km, max_radius = performance(air["v"], time_h)

    meets = max_radius >= radius_req

    components = component_recommendations(propulsion, budget)

    ai = gemini_summary(mission, propulsion, MTOW, max_radius)

    return jsonify({
        "mission": {
            "missionType": mission,
            "propulsion": propulsion,
            "meetsRadius": meets,
            "requiredRadius": radius_req,
            "achievableRadius": round(max_radius, 1)
        },
        "gmgroup": {
            "recommendedProp": components
        },
        "calculations": {
            "drag_N": round(D, 2),
            "power_W": round(P, 1),
            "energy_Wh": round(Wh, 1) if Wh else None,
            "fuelOrBattery_kg": round(energy_mass, 2),
            "mtow_kg": round(MTOW, 2),
            "range_km": round(range_km, 1)
        },
        "ai": {
            "summary": ai
        }
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
