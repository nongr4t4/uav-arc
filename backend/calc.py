from flask import Flask, request, jsonify
from flask_cors import CORS
import os
import math
import requests

app = Flask(__name__)
CORS(app)

GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-preview-09-2025:generateContent"
API_KEY = os.environ.get("GEMINI_API_KEY", "")

RHO = 1.225  # густина повітря на рівні моря


# -------------------------------------------------
# 1) КЛАСИФІКАЦІЯ МІСІЇ / ТИПУ БПЛА
# -------------------------------------------------
def classify_mission(time_h: float, radius_km: float) -> str:
    """
    Тактична / оперативна / стратегічна глибина за вимогами до часу та радіуса.
    """
    if time_h <= 4 and radius_km <= 50:
        return "tactical"
    if time_h <= 27 and radius_km <= 300:
        return "operational"
    return "strategic"


UAV_TYPES = {
    "fpv_multirotor": {
        "name": "FPV / мультиротор",
        "description": "Вертикальний зліт і посадка, висока маневровість, мала дальність польоту.",
    },
    "fixed_wing": {
        "name": "Літак (фіксоване крило)",
        "description": "Енергоефективна платформа для розвідки та патрулювання на середніх і великих дальностях.",
    },
    "flying_wing": {
        "name": "Літаюче крило",
        "description": "Аеродинамічно ефективна платформа з великим відношенням дальності до маси, часто для камікадзе-дронів.",
    },
    "loitering_munition": {
        "name": "Камікадзе-дрон (барражуючі боєприпаси)",
        "description": "Одноразова платформа з максимальною дальністю і простотою конструкції.",
    },
}


def select_uav_type(mission: str, payload_kg: float, radius_km: float, time_h: float) -> str:
    """
    Вибір логічного типу БПЛА на основі місії, корисного навантаження, радіуса та часу.
    """
    # Дуже короткі місії з малим радіусом і малою масою → FPV / мультиротор
    if mission == "tactical":
        if time_h <= 1.5 and radius_km <= 15 and payload_kg <= 2:
            return "fpv_multirotor"
        # Більший радіус, але ще тактична глибина → малий літак
        return "fixed_wing"

    # Оперативна глибина → переважно фіксоване крило
    if mission == "operational":
        if payload_kg >= 20 or radius_km > 80:
            return "fixed_wing"
        else:
            return "flying_wing"

    # Стратегічна глибина → літаюче крило / камікадзе
    if radius_km >= 400 and payload_kg >= 20:
        return "loitering_munition"
    return "flying_wing"


def choose_propulsion(mission_type: str, low_noise: bool, budget: float, uav_type: str) -> str:
    """
    Вибір типу силової установки (електрика / поршневий / турбіна).
    """
    # Тактична мала платформа з вимогою тихості → електрика
    if mission_type == "tactical":
        if low_noise and budget < 7000:
            return "electric"
        # Без жорсткої вимоги до шуму і дуже великий радіус для тактики → поршень
        return "piston_engine"

    # Оперативна глибина → здебільшого поршневий двигун
    if mission_type == "operational":
        # Якщо бюджет дикий і немає вимоги до ціни, можна вже й турбіну, але тримаємо простіше
        if budget > 100000 and not low_noise:
            return "turbine"
        return "piston_engine"

    # Стратегічна: великі дистанції → турбіна, якщо бюджет дозволяє
    if budget > 50000:
        return "turbine"
    return "piston_engine"


# -------------------------------------------------
# 2) АЕРОДИНАМІЧНІ ШАБЛОНИ ДЛЯ МІСІЇ
# -------------------------------------------------
TEMPLATES = {
    # типові значення для "базового" апарата без батареї/палива та корисного навантаження
    "tactical": {"emptyMass_kg": 2.0, "Cd": 0.04, "S": 0.8, "v": 20},   # малий тактичний літак
    "operational": {"emptyMass_kg": 50.0, "Cd": 0.04, "S": 8.0, "v": 60},
    "strategic": {"emptyMass_kg": 150.0, "Cd": 0.035, "S": 18.0, "v": 120},
}

PROP = {
    "electric": {"eta_prop": 0.8, "eta_sys": 0.8, "battery_wh_kg": 220},
    "piston_engine": {"eta_prop": 0.8, "bsfc": 0.25},  # kg/kWh
    "turbine": {"eta_prop": 0.85, "bsfc": 0.3},
}

UAV_AERO_FACTORS = {
    # множники до базового шаблону місії
    "fpv_multirotor": {"v_factor": 0.5, "Cd_factor": 1.6, "S_factor": 0.6},
    "fixed_wing": {"v_factor": 0.7, "Cd_factor": 1.0, "S_factor": 0.8},
    "flying_wing": {"v_factor": 0.9, "Cd_factor": 0.8, "S_factor": 0.7},
    "loitering_munition": {"v_factor": 1.0, "Cd_factor": 0.9, "S_factor": 0.7},
}


# -------------------------------------------------
# 3) БАЗОВІ ФУНКЦІЇ РОЗРАХУНКУ
# -------------------------------------------------
def aerodynamic_drag(rho: float, v: float, S: float, Cd: float) -> float:
    return 0.5 * rho * v * v * S * Cd


def cruise_power(T: float, v: float, eta: float) -> float:
    return T * v / eta


def electric_energy(power_W: float, t_h: float, wh_kg: float, eta_sys: float):
    """
    Повертає (енергія Wh, маса батареї кг).
    """
    Wh = (power_W * t_h) / eta_sys
    mass = Wh / wh_kg
    return Wh, mass


def fuel_mass(power_W: float, t_h: float, bsfc: float) -> float:
    """
    Маса палива (кг) при заданій потужності, часі та питомій витраті (kg/kWh).
    """
    return t_h * bsfc * (power_W / 1000.0)


def performance(v_mps: float, time_h: float):
    dist = v_mps * time_h * 3.6  # км
    rad = dist / 2.0
    return dist, rad


# -------------------------------------------------
# 4) ПІДБІР КОМПОНЕНТІВ ЗАЛЕЖНО ВІД МАСИ / МІСІЇ
# -------------------------------------------------
def recommend_electric_components(mtow_kg: float, mission: str, uav_type: str):
    """
    Грубий підбір електричної ГМГ по класах маси.
    """
    if mtow_kg <= 6:
        return {
            "engine": {
                "model": "T-Motor MN4014-9 KV400",
                "price": 150,
                "reason": "Легкий безщітковий двигун з запасом по тязі для БПЛА масою до 5–6 кг.",
            },
            "propeller": {
                "model": "T-Motor 15x5 CF",
                "price": 80,
                "reason": "Великий діаметр і малий крок для тихого та ефективного крейсерського польоту на малих швидкостях.",
            },
            "electronics": {
                "model": "ESC 40–60A + автопілот типу Pixhawk/Cube",
                "price": 250,
                "reason": "Забезпечує плавне керування тягою та стабілізацію для розвідувального крила.",
            },
            "battery": {
                "model": "Li-Ion 6S 20Ah (~444Wh)",
                "price": 300,
                "reason": "Висока енергоємність для досягнення 2+ год польоту.",
            },
        }

    if mtow_kg <= 25:
        return {
            "engine": {
                "model": "T-Motor U8 KV150",
                "price": 450,
                "reason": "Потужний мотор для середнього крила/VTOL з хорошим ККД.",
            },
            "propeller": {
                "model": "28x9.2 CF",
                "price": 220,
                "reason": "Діаметр ~28\" для високої статичної тяги та економного крейсеру.",
            },
            "electronics": {
                "model": "ESC 80–100A + Cube Orange",
                "price": 600,
                "reason": "Надійна електроніка для довготривалих місій.",
            },
            "battery": {
                "model": "Li-Ion 12S 30Ah (~1330Wh)",
                "price": 800,
                "reason": "Достатній запас енергії для 4+ годин польоту при середньому споживанні.",
            },
        }

    # великі електрички
    return {
        "engine": {
            "model": "T-Motor U13 KV100",
            "price": 800,
            "reason": "Високий крутний момент для важких апаратів.",
        },
        "propeller": {
            "model": "30x10 CF",
            "price": 260,
            "reason": "Баланс тяги та швидкості для великого крила.",
        },
        "electronics": {
            "model": "ESC 120A HV + професійний автопілот",
            "price": 900,
            "reason": "Необхідні для безпечної роботи важкого БПЛА.",
        },
        "battery": {
            "model": "Li-Ion 12–14S 40Ah",
            "price": 1200,
            "reason": "Забезпечує тривалий політ при значній масі.",
        },
    }


def recommend_piston_components(mtow_kg: float, mission: str):
    if mtow_kg <= 80:
        return {
            "engine": {
                "model": "DLE-60 Twin",
                "price": 800,
                "reason": "Легкий двоциліндровий бензиновий двигун для БПЛА 20–80 кг.",
            },
            "propeller": {
                "model": "24x10 дерев'яний",
                "price": 200,
                "reason": "Забезпечує достатню тягу при помірних обертах і прийнятному шумі.",
            },
            "electronics": {
                "model": "Pixhawk / Cube з опцією запалювання",
                "price": 500,
                "reason": "Стабільний автопілот з підтримкою ДВЗ.",
            },
            "battery": {
                "model": "LiPo 4–6S 5–10Ah (живлення бортової мережі)",
                "price": 150,
                "reason": "Живить тільки електроніку, основна енергія — у бензині.",
            },
        }

    return {
        "engine": {
            "model": "Rotax 912 iS",
            "price": 25000,
            "reason": "Надійний чотиритактний двигун для апаратів 150–600 кг.",
        },
        "propeller": {
            "model": "2.0m композитний, змінного кроку",
            "price": 4500,
            "reason": "Високий ККД у широкому діапазоні режимів польоту.",
        },
        "electronics": {
            "model": "Професійний автопілот з двоканальним живленням",
            "price": 3000,
            "reason": "Необхідний рівень надійності для операційних та стратегічних місій.",
        },
        "battery": {
            "model": "LiFePO4 12V 10–20Ah",
            "price": 300,
            "reason": "Стабільне живлення бортових систем.",
        },
    }


def recommend_turbine_components(mtow_kg: float, mission: str):
    return {
        "engine": {
            "model": "PBS TJ40",
            "price": 45000,
            "reason": "Легкий турбореактивний двигун для високошвидкісних та висотних платформ.",
        },
        "propeller": {
            "model": "N/A",
            "price": 0,
            "reason": "Реактивний двигун не використовує пропелер.",
        },
        "electronics": {
            "model": "Cube Orange+ з підтримкою реактивної СУ",
            "price": 2000,
            "reason": "Потрібен для безпечного керування реактивною платформою.",
        },
        "battery": {
            "model": "LiPo/Li-Ion 6–8S 10–20Ah",
            "price": 400,
            "reason": "Живлення автопілота та сервісних систем.",
        },
    }


def component_recommendations(propulsion: str, budget: float, mission: str, uav_type: str, mtow_kg: float):
    """
    Вибір конкретної ГМГ за типом силової установки та класом маси.
    """
    if propulsion == "electric":
        return recommend_electric_components(mtow_kg, mission, uav_type)
    if propulsion == "piston_engine":
        return recommend_piston_components(mtow_kg, mission)
    return recommend_turbine_components(mtow_kg, mission)


# -------------------------------------------------
# 5) AI / ТЕКСТОВІ ВИСНОВКИ
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

    payload = {"contents": [{"parts": [{"text": prompt}]}]}

    r = requests.post(f"{GEMINI_API_URL}?key={API_KEY}", json=payload).json()
    try:
        return r["candidates"][0]["content"]["parts"][0]["text"]
    except Exception:
        return "AI не зміг сформувати висновок."


def engineering_explanation(uav_type: str,
                            mission: str,
                            propulsion: str,
                            mtow_kg: float,
                            time_h: float,
                            max_radius_km: float,
                            components: dict) -> str:
    """
    Людське технічне пояснення (8–12 речень), чому обрано саме таку конфігурацію.
    """
    uav_name = UAV_TYPES[uav_type]["name"]
    eng = components.get("engine", {})
    prop = components.get("propeller", {})
    batt = components.get("battery", {})

    engine_model = eng.get("model", "двигун")
    prop_model = prop.get("model", "пропелер")
    batt_model = batt.get("model", "акумулятор / паливна система")

    mission_human = {
        "tactical": "тактичної глибини",
        "operational": "оперативної глибини",
        "strategic": "стратегічної / стратегічно-оперативної глибини",
    }.get(mission, mission)

    propulsion_human = {
        "electric": "електричну силову установку",
        "piston_engine": "поршневий двигун внутрішнього згоряння",
        "turbine": "турбінну / реактивну силову установку",
    }.get(propulsion, propulsion)

    text = []
    text.append(
        f"Для даних вхідних вимог обрано платформу типу «{uav_name}», "
        f"оскільки вона найкраще відповідає профілю місії {mission_human} за дальністю та часом польоту."
    )
    text.append(
        f"Тип силової установки — {propulsion_human}, що забезпечує достатній запас потужності при злітній масі "
        f"приблизно {mtow_kg:.1f} кг і дозволяє відпрацювати місію тривалістю близько {time_h:.1f} годин."
    )
    text.append(
        f"У якості двигуна запропоновано {engine_model}, який має адекватне співвідношення маси та доступної тяги "
        f"для даного класу БПЛА."
    )
    text.append(
        f"Пропелер {prop_model} підібрано під робочу потужність двигуна і крейсерський режим: "
        f"великий діаметр і помірний крок забезпечують хороший ККД та відносно низький шум."
    )
    text.append(
        f"Акумулятор/паливна система «{batt_model}» дає необхідний запас енергії для досягнення розрахункового "
        f"радіуса дії ≈ {max_radius_km:.1f} км з резервом на маневрування та повернення."
    )
    text.append(
        "Така конфігурація зберігає прийнятний рівень маси гвинтомоторної групи порівняно з загальною масою літака, "
        "що важливо для злітно-посадкових характеристик і стійкості у польоті."
    )
    text.append(
        "Альтернативи у вигляді важчого двигуна або більш ємної батареї збільшили б тривалість польоту, "
        "але суттєво підняли б MTOW і ускладнили запуск та експлуатацію."
    )
    text.append(
        "Навпаки, використання менш потужного двигуна або менших акумуляторів призвело б до недостатнього запасу тяги "
        "і скорочення корисного радіуса, що робить обраний варіант оптимальним компромісом."
    )

    return " ".join(text)


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

    # 1) Класифікація місії та типу платформи
    mission = classify_mission(time_h, radius_req)
    uav_type = select_uav_type(mission, payload, radius_req, time_h)

    # 2) Параметри аеродинаміки для обраної місії + типу БПЛА
    base = TEMPLATES[mission]
    factors = UAV_AERO_FACTORS[uav_type]

    v = base["v"] * factors["v_factor"]        # м/с
    S = base["S"] * factors["S_factor"]        # площа крила
    Cd = base["Cd"] * factors["Cd_factor"]
    empty_mass = base["emptyMass_kg"]

    # 3) Вибір типу силової установки
    propulsion = choose_propulsion(mission, lowNoise, budget, uav_type)
    prop_data = PROP[propulsion]

    # 4) Аеродинамічний опір / тяга / потужність
    D = aerodynamic_drag(RHO, v, S, Cd)
    T = D
    P = cruise_power(T, v, prop_data["eta_prop"])

    # 5) Енергетика: батарея / паливо
    if propulsion == "electric":
        Wh, energy_mass = electric_energy(P, time_h, prop_data["battery_wh_kg"], prop_data["eta_sys"])
    else:
        Wh = None
        energy_mass = fuel_mass(P, time_h, prop_data["bsfc"])

    # 6) Маса / дальність / радіус
    MTOW = empty_mass + payload + energy_mass
    range_km, max_radius = performance(v, time_h)

    meets = max_radius >= radius_req

    # 7) Підбір компонентів за MTOW
    components = component_recommendations(propulsion, budget, mission, uav_type, MTOW)

    # 8) AI + людський інженерний текст
    ai = gemini_summary(mission, propulsion, MTOW, max_radius)
    explanation = engineering_explanation(uav_type, mission, propulsion, MTOW, time_h, max_radius, components)

    return jsonify({
        "mission": {
            "missionType": mission,
            "propulsion": propulsion,
            "meetsRadius": meets,
            "requiredRadius": radius_req,
            "achievableRadius": round(max_radius, 1),
        },
        "uav": {
            "type": uav_type,
            "name": UAV_TYPES[uav_type]["name"],
            "description": UAV_TYPES[uav_type]["description"],
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
            "range_km": round(range_km, 1),
            "cruise_speed_mps": round(v, 2),
            "wing_area_m2": round(S, 3),
            "Cd": round(Cd, 4),
        },
        "analysis": {
            "designExplanation": explanation
        },
        "ai": {
            "summary": ai
        }
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
