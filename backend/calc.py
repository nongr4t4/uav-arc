from flask import Flask, request, jsonify
from flask_cors import CORS
import os
import requests # Використовуємо requests для HTTP-запитів до Gemini API

app = Flask(__name__)
CORS(app)

# -------------------------
# КОНФІГУРАЦІЯ GEMINI API
# -------------------------
GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-preview-09-2025:generateContent"
# Залиште порожнім, якщо не використовуєте змінну середовища
API_KEY = os.environ.get("GEMINI_API_KEY", "") 

# -------------------------
# ЛОГІКА (СПРОЩЕНА)
# -------------------------

def classify_mission(time_h, radius_km):
    if time_h <= 4 and radius_km <= 50:
        return "tactical"
    if time_h <= 27 and radius_km <= 300:
        return "operational"
    return "strategic"


def choose_propulsion(mission_type, low_noise, budget):
    # Примітка: оригінальна логіка містила client.responses.create, яка не існує. 
    # Я зберігаю оригінальну логіку вибору, але припускаю, що "client" був
    # Placeholder для чогось іншого.
    if mission_type == "tactical" and low_noise and budget < 7000:
        return "electric"
    if mission_type == "operational":
        return "piston_engine"
    return "turbine"


TEMPLATES = {
    "tactical": {"emptyMass_kg": 2.0, "wingArea_m2": 0.8, "Cd": 0.035, "cruiseSpeed_mps": 20, "rho": 1.225},
    "operational": {"emptyMass_kg": 50, "wingArea_m2": 8.0, "Cd": 0.04, "cruiseSpeed_mps": 60, "rho": 1.225},
    "strategic": {"emptyMass_kg": 1500, "wingArea_m2": 40.0, "Cd": 0.03, "cruiseSpeed_mps": 150, "rho": 1.225}
}

PROP = {
    "electric": {"propEfficiency": 0.8, "systemEfficiency": 0.8, "batteryDensity_Wh_kg": 220},
    "piston_engine": {"propEfficiency": 0.8, "BSFC_kg_kWh": 0.25},
    "turbine": {"propEfficiency": 0.85, "BSFC_kg_kWh": 0.3}
}


def drag_and_thrust(rho, v, S, Cd):
    D = 0.5 * rho * v * v * S * Cd
    return D, D


def cruise_power(thrust, v, eta):
    return thrust * v / eta


def electric_energy_and_mass(power_W, time_h, density_Wh_kg, system_eta):
    Wh = power_W * time_h / system_eta
    mass = Wh / density_Wh_kg
    return Wh, mass


def performance(v_mps, time_h):
    dist = v_mps * time_h * 3.6 # v_mps * time_s / 1000
    return dist, dist / 2



# -------------------------
# GEMINI API ВИКЛИК
# -------------------------

def gemini_explanation(mission, propulsion, mass, radius):
    """Генерує пояснення за допомогою Gemini 2.5 Flash."""
    
    if not API_KEY:
        return "Помилка: API ключ Gemini не налаштований."

    system_prompt = "Ти інженер БПЛА. Поясни людською мовою, використовуючи лише 3–5 речень, українською мовою."
    
    user_query = f"""
    Проаналізуй наступні параметри конфігурації БПЛА і поясни їх:
    - Тип місії: {mission}
    - Тип двигуна: {propulsion}
    - Злітна маса: {mass:.2f} кг
    - Розрахунковий радіус дії: {radius:.1f} км
    """
    
    payload = {
        "contents": [
            {"parts": [{"text": user_query}]}
        ],
        "systemInstruction": {
            "parts": [{"text": system_prompt}]
        },
        # Вмикаємо Google Search Grounding для більш обґрунтованої відповіді
        "tools": [{"google_search": {}}],
    }

    try:
        full_url = f"{GEMINI_API_URL}?key={API_KEY}"
        
        # Виконуємо POST запит до Gemini API
        response = requests.post(full_url, json=payload)
        response.raise_for_status() # Обробка помилок HTTP
        
        result = response.json()
        
        # Видобуваємо згенерований текст
        candidate = result.get('candidates', [{}])[0]
        text_part = candidate.get('content', {}).get('parts', [{}])[0]
        ai_text = text_part.get('text', 'Не вдалося отримати пояснення від AI.')

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

    # Перетворення радіусу з km на m для правильного використання в classify_mission
    time_h = float(data["timeHours"])
    # Перетворення радіусу з km на m для логіки, хоча логіка в km, залишаємо km
    radius = float(data["radiusKm"]) 
    payload = float(data["payloadKg"])
    lowNoise = bool(data["lowNoise"])
    budget = float(data["budget"])

    mission_type = classify_mission(time_h, radius)
    propulsion_type = choose_propulsion(mission_type, lowNoise, budget)

    air = TEMPLATES[mission_type]
    prop = PROP[propulsion_type]

    # Розрахунок тяги та опору (D=T)
    D, T = drag_and_thrust(air["rho"], air["cruiseSpeed_mps"], air["wingArea_m2"], air["Cd"])
    # Розрахунок необхідної потужності
    P = cruise_power(T, air["cruiseSpeed_mps"], prop["propEfficiency"])

    if propulsion_type == "electric":
        required_Wh, batt_mass = electric_energy_and_mass(
            P, time_h, prop["batteryDensity_Wh_kg"], prop["systemEfficiency"]
        )
    else:
        # Для двигунів внутрішнього згоряння
        # P / 1000 перетворює Вт у кВт
        fuel_mass = time_h * prop["BSFC_kg_kWh"] * (P / 1000) 
        required_Wh = None
        batt_mass = fuel_mass

    takeoff_mass = air["emptyMass_kg"] + payload + batt_mass

    # Розрахунок реальної дальності/радіусу на основі параметрів шаблону та часу
    total_dist_km, radius_est_km = performance(air["cruiseSpeed_mps"], time_h)

    # Виклик Gemini для пояснення
    ai_expl = gemini_explanation(mission_type, propulsion_type, takeoff_mass, radius_est_km)

    return jsonify({
        "mission": {
            "missionType": mission_type,
            "recommendedPropulsion": propulsion_type
        },
        "calculations": {
            "power": {"cruisePower_W": round(P, 2)},
            "energy": {
                "requiredEnergy_Wh": round(required_Wh, 2) if required_Wh is not None else None,
                "batteryOrFuelMass_kg": round(batt_mass, 2)
            },
            "mass": {"takeoffMass_kg": round(takeoff_mass, 2)},
            "performance": {
                "achievableRadius_km": round(radius_est_km, 1),
                "achievableRange_km": round(total_dist_km, 1)
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
    # Уникаємо використання gunicorn для простоти, як в оригінальному коді
    app.run(host="0.0.0.0", port=port, debug=True)
