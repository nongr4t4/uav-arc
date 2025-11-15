from flask import Flask, request, jsonify
from flask_cors import CORS
import math
import os
from openai import OpenAI

app = Flask(__name__)
CORS(app)

client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

# -----------------------------
# Mission logic
# -----------------------------

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


TEMPLATES = {
    "tactical": {
        "emptyMass_kg": 2.0,
        "wingArea_m2": 0.8,
        "Cd": 0.035,
        "cruiseSpeed_mps": 20,
        "rho": 1.225
    },
    "operational": {
        "emptyMass_kg": 50,
        "wingArea_m2": 8.0,
        "Cd": 0.04,
        "cruiseSpeed_mps": 60,
        "rho": 1.225
    },
    "strategic": {
        "emptyMass_kg": 1500,
        "wingArea_m2": 40.0,
        "Cd": 0.03,
        "cruiseSpeed_mps": 150,
        "rho": 1.225
    }
}

PROP = {
    "electric": {
        "propEfficiency": 0.8,
        "systemEfficiency": 0.8,
        "batteryDensity_Wh_kg": 220
    },
    "piston_engine": {
        "propEfficiency": 0.8,
        "BSFC_kg_kWh": 0.25
    },
    "turbine": {
        "propEfficiency": 0.85,
        "BSFC_kg_kWh": 0.3
    }
}


def drag_and_thrust(rho, v, S, Cd):
    D = 0.5 * rho * v * v * S * Cd
    return D, D


def cruise_power(thrust, v, eta):
    return thrust * v / eta


def electric_energy_and_mass(power_W, time_h, density_Wh_kg, system_eta):
    required_Wh = power_W * time_h / system_eta
    mass_kg = required_Wh / density_Wh_kg
    return required_Wh, mass_kg


def performance(v_mps, time_h):
    distance_km = v_mps * time_h / 1000
    return distance_km, distance_km / 2


def chatgpt_explanation(mission, propulsion, mass, radius):
    prompt = f"""
Ти інженер БПЛА. Поясни людською мовою:

- тип місії: {mission}
- обрана ГМГ: {propulsion}
- маса апарата: {mass:.2f} кг
- досяжний радіус: {radius:.1f} км

Напиши короткий висновок у 3–5 реченнях.
"""

    res = client.responses.create(
        model="gpt-4.1",
        input=prompt
    )

    return res.output_text


# -----------------------------
# API ENDPOINT
# -----------------------------

@app.route("/api/configure", methods=["POST"])
def configure():
    input_data = request.json

    time_h = float(input_data["timeHours"])
    radius_km = float(input_data["radiusKm"])
    payload_kg = float(input_data["payloadKg"])
    low_noise = bool(input_data["lowNoise"])
    budget = float(input_data["budget"])

    mission_type = classify_mission(time_h, radius_km)
    propulsion_type = choose_propulsion(mission_type, low_noise, budget)

    air = TEMPLATES[mission_type]
    prop = PROP[propulsion_type]

    D, T = drag_and_thrust(
        rho=air["rho"],
        v=air["cruiseSpeed_mps"],
        S=air["wingArea_m2"],
        Cd=air["Cd"]
    )

    P = cruise_power(
        thrust=T,
        v=air["cruiseSpeed_mps"],
        eta=prop["propEfficiency"]
    )

    if propulsion_type == "electric":
        required_Wh, battery_mass = electric_energy_and_mass(
            P, time_h, prop["batteryDensity_Wh_kg"], prop["systemEfficiency"]
        )
    else:
        fuel_mass = time_h * prop["BSFC_kg_kWh"] * (P / 1000)
        required_Wh = None
        battery_mass = fuel_mass

    takeoff_mass = air["emptyMass_kg"] + payload_kg + battery_mass

    total_dist, radius_est = performance(
        air["cruiseSpeed_mps"], time_h
    )

    ai_expl = chatgpt_explanation(
        mission_type, propulsion_type, takeoff_mass, radius_est
    )

    return jsonify({
        "mission": {
            "missionType": mission_type,
            "recommendedPropulsion": propulsion_type
        },
        "calculations": {
            "aerodynamics": {
                "drag_N": D,
                "thrust_N": T
            },
            "power": {
                "cruisePower_W": P
            },
            "energy": {
                "requiredEnergy_Wh": required_Wh,
                "batteryOrFuelMass_kg": battery_mass
            },
            "mass": {
                "takeoffMass_kg": takeoff_mass
            },
            "performance": {
                "achievableTime_h": time_h,
                "achievableRadius_km": radius_est,
                "achievableRange_km": total_dist
            }
        },
        "aiComment": ai_expl
    })


# -----------------------------
# RUN SERVER (локально)
# -----------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 443))
    app.run(host="0.0.0.0", port=port)
