from flask import Flask, request, jsonify
from flask_cors import CORS
import os
import requests  # Використовуємо requests для HTTP-запитів до Gemini API

app = Flask(__name__)
CORS(app)

# -------------------------
# КОНФІГУРАЦІЯ GEMINI API
# -------------------------
GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-preview-09-2025:generateContent"
API_KEY = os.environ.get("GEMINI_API_KEY", "")  # 🔥 НЕ ЧІПАЮ

# -------------------------
# ЛОГІКА (КОРИГУВАННЯ)
# -------------------------

def classify_mission(time_h, radius_km, payload_kg):
    """
    Класифікація місії:
    - спочатку по часу/радіусу (тактична/оперативна/стратегічна),
    - потім корекція по корисному навантаженню (щоб не було 100 кг на "тактичному").
    """
    # Базова класифікація за глибиною
    if time_h <= 4 and radius_km <= 50:
        base_type = "tactical"
    elif time_h <= 27 and radius_km <= 300:
        base_type = "operational"
    else:
        base_type = "strategic"

    # Корекція по payload:
    # 10+ кг → мінімум оперативна, 100+ кг → стратегічна
    if payload_kg >= 100:
        return "strategic"
    if payload_kg >= 10 and base_type == "tactical":
        return "operational"

    return base_type


def choose_propulsion(mission_type, low_noise, budget):
    """
    Вибір типу силової установки.
    """
    if mission_type == "tactical" and low_noise and budget < 7000:
        return "electric"
    if mission_type == "operational":
        return "piston_engine"
    return "turbine"


# Шаблонні аеродинамічні параметри (геометрія, а не маса)
TEMPLATES = {
    "tactical": {
        "emptyMass_kg": 2.0,          # Базова структурна маса для легкого БПЛА
        "wingArea_m2": 0.8,
        "Cd": 0.035,
        "cruiseSpeed_mps": 20,
        "rho": 1.225
    },
    "operational": {
        "emptyMass_kg": 50.0,         # Базова структурна маса для оперативного
        "wingArea_m2": 8.0,
        "Cd": 0.04,
        "cruiseSpeed_mps": 60,
        "rho": 1.225
    },
    "strategic": {
        "emptyMass_kg": 1500.0,       # Базова структурна маса для стратегічного
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
        "batteryDensity_Wh_kg": 220  # Wh/кг
    },
    "piston_engine": {
        "propEfficiency": 0.8,
        "BSFC_kg_kWh": 0.25          # кг/кВт·год
    },
    "turbine": {
        "propEfficiency": 0.85,
        "BSFC_kg_kWh": 0.3           # кг/кВт·год
    }
}


def drag_and_thrust(rho, v, S, Cd):
    """
    Аеродинамічний опір і тяга в крейсері:
    D = 0.5 * ρ * V^2 * S * Cd
    В сталому горизонтальному польоті T = D.
    """
    D = 0.5 * rho * v * v * S * Cd
    return D, D


def cruise_power(thrust, v, eta):
    """
    Необхідна потужність:
    P = T * V / η
    """
    return thrust * v / eta


def electric_energy_and_mass(power_W, time_h, density_Wh_kg, system_eta):
    """
    Для електро:
    t = (E * η) / P  →  E = (P * t) / η
    Масу батареї: m = E / ρ_бат
    """
    required_Wh = power_W * time_h / system_eta
    mass = required_Wh / density_Wh_kg
    return required_Wh, mass


def performance(v_mps, time_h):
    """
    Дальність і радіус:
    V [м/с] → км/год = V * 3.6
    Range_km = V_kmh * t
    Radius = Range / 2
    """
    range_km = v_mps * 3.6 * time_h
    radius_km = range_km / 2.0
    return range_km, radius_km


# -------------------------
# GEMINI API ВИКЛИК
# -------------------------

def gemini_explanation(mission, propulsion, payload, empty_mass, energy_mass, radius):
    """
    Генерує стислий технічний опис конфігурації БПЛА.
    """

    if not API_KEY:
        return "Помилка: API ключ Gemini не налаштований."

    system_prompt = (
        "Ти досвідчений інженер-конструктор БПЛА. "
        "Зроби стислий технічний висновок у 3–5 реченнях українською мовою. "
        "Оціни адекватність: типу місії, типу двигуна, співвідношення корисного "
        "навантаження до злітної маси та реалістичність радіуса дії. "
        "Стиль — інженерний, без пафосу."
    )

    user_query = f"""
    Проаналізуй конфігурацію БПЛА:
    - Тип місії: {mission}
    - Тип двигуна: {propulsion}
    - Корисне навантаження (боєголовка/сенсори): {payload:.1f} кг
    - Структурна маса планера (без батареї/палива): {empty_mass:.1f} кг
    - Маса енергетичної системи (АКБ/паливо): {energy_mass:.1f} кг
    - Розрахунковий радіус дії: {radius:.1f} км

    Зроби короткий технічний висновок: чи виглядає така конфігурація збалансованою,
    де основні вузькі місця, та для яких задач вона підходить найкраще.
    """

    payload_body = {
        "contents": [
            {"parts": [{"text": user_query}]}
        ],
        "systemInstruction": {
            "parts": [{"text": system_prompt}]
        },
    }

    try:
        full_url = f"{GEMINI_API_URL}?key={API_KEY}"
        response = requests.post(full_url, json=payload_body)
        response.raise_for_status()

        result = response.json()
        candidate = result.get("candidates", [{}])[0]
        text_part = candidate.get("content", {}).get("parts", [{}])[0]
        ai_text = text_part.get("text", "Не вдалося отримати пояснення від AI.")

        return ai_text

    except requests.exceptions.RequestException as e:
        print(f"Помилка виклику Gemini API: {e}")
        return f"Помилка зв'язку з AI сервісом: {e}"
    except Exception as e:
        print(f"Виникла несподівана помилка: {e}")
        return "Виникла несподівана помилка при обробці відповіді AI."


# -------------------------
# API
# -------------------------

@app.route("/api/configure", methods=["POST"])
def configure():

    data = request.get_json()

    time_h = float(data["timeHours"])
    radius_req_km = float(data["radiusKm"])
    payload = float(data["payloadKg"])       # це ТІЛЬКИ корисне навантаження (вибухівка/сенсори)
    lowNoise = bool(data["lowNoise"])
    budget = float(data["budget"])

    # 1. Класифікація місії (з урахуванням payload)
    mission_type = classify_mission(time_h, radius_req_km, payload)
    propulsion_type = choose_propulsion(mission_type, lowNoise, budget)

    air = TEMPLATES[mission_type]
    prop = PROP[propulsion_type]

    # 2. Аеродинаміка
    D, T = drag_and_thrust(
        air["rho"],
        air["cruiseSpeed_mps"],
        air["wingArea_m2"],
        air["Cd"]
    )

    # 3. Потрібна потужність
    P = cruise_power(T, air["cruiseSpeed_mps"], prop["propEfficiency"])

    # 4. Енергосистема: батарея / паливо
    if propulsion_type == "electric":
        required_Wh, batt_mass = electric_energy_and_mass(
            P,
            time_h,
            prop["batteryDensity_Wh_kg"],
            prop["systemEfficiency"]
        )
        # Мінімальна маса батареї (щоб не було "0.5 кг батарея на 2 години")
        if batt_mass < 3.0:
            batt_mass = 3.0
            # енергії тоді більше, ніж треба; для спрощення не перераховуємо час.
    else:
        # Для ДВЗ: оцінка маси палива
        # fuel_mass = t * BSFC * P[kW]
        fuel_mass = time_h * prop["BSFC_kg_kWh"] * (P / 1000.0)
        required_Wh = None
        batt_mass = fuel_mass

    # 5. Структурна маса
    # Беремо базову масу шаблону і додаємо корекцію для великих payload
    base_empty = air["emptyMass_kg"]
    # Якщо payload значно більший за базовий планер → масштабуємо
    if payload > base_empty:
        # дуже проста модель: empty_mass ≈ max(base_empty, 0.4 * (payload + batt_mass))
        empty_mass = max(base_empty, 0.4 * (payload + batt_mass))
    else:
        empty_mass = base_empty

    # 6. Злітна маса (MTOW)
    takeoff_mass = empty_mass + payload + batt_mass

    # 7. Дальність / реальний радіус (по крейсерській швидкості та часу)
    total_dist_km, radius_est_km = performance(air["cruiseSpeed_mps"], time_h)

    # 8. Виклик Gemini для технічного висновку
    ai_expl = gemini_explanation(
        mission_type,
        propulsion_type,
        payload,
        empty_mass,
        batt_mass,
        radius_est_km
    )

    return jsonify({
        "mission": {
            "missionType": mission_type,
            "recommendedPropulsion": propulsion_type
        },
        "calculations": {
            "power": {
                "cruisePower_W": round(P, 2)
            },
            "energy": {
                "requiredEnergy_Wh": round(required_Wh, 2) if required_Wh is not None else None,
                "batteryOrFuelMass_kg": round(batt_mass, 2)
            },
            "mass": {
                "emptyMass_kg": round(empty_mass, 2),
                "payloadMass_kg": round(payload, 2),
                "takeoffMass_kg": round(takeoff_mass, 2)
            },
            "performance": {
                "achievableRadius_km": round(radius_est_km, 1),
                "achievableRange_km": round(total_dist_km, 1)
            },
            "requirementsCheck": {
                "meetsTime": True,
                "meetsRadius": radius_est_km >= radius_req_km
            }
        },
        "aiComment": ai_expl
    })


# -------------------------
# FLASK RUN
# -------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print("Running Flask on port", port)
    app.run(host="0.0.0.0", port=port, debug=True)
