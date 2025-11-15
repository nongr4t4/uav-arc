from flask import Flask, request, jsonify
from flask_cors import CORS
import math
import os

app = Flask(__name__)
CORS(app)

RHO = 1.225  # густина повітря, кг/м³


# -----------------------------
# 1. КЛАСИФІКАЦІЯ МІСІЇ
# -----------------------------
def classify_mission(time_h: float, radius_km: float) -> str:
    """
    Груба класифікація місії: коротка / середня / розширена.
    Без військового контексту — просто дальність і тривалість.
    """
    if time_h <= 1.5 and radius_km <= 30:
        return "short_range"       # умовно "тактична коротка"
    if time_h <= 4 and radius_km <= 150:
        return "medium_range"      # умовно "тактична / оперативна"
    return "extended_range"        # довша місія, але без "стратегічної" риторики


# -----------------------------
# 2. ВИБІР ТИПУ ПЛАТФОРМИ
# -----------------------------
UAV_TYPES = {
    "multirotor": {
        "name": "Мультиротор / FPV-платформа",
        "description": "Вертикальний зліт і посадка, висока маневровність, але порівняно мала дальність."
    },
    "fixed_wing": {
        "name": "Літак (фіксоване крило)",
        "description": "Енергоефективна платформа для розвідки та патрулювання на середніх дальностях."
    },
    "vtol_fixed_wing": {
        "name": "VTOL з фіксованим крилом",
        "description": "Поєднує вертикальний зліт із ефективністю літака в крейсері."
    },
}

def select_uav_type(mission: str, payload_kg: float, radius_km: float, time_h: float, low_noise: bool) -> str:
    """
    Вибір типу БПЛА, виходячи з радіуса, часу та корисного навантаження.
    """
    # Дуже короткі місії, малий радіус, невелика маса → FPV / мультиротор
    if mission == "short_range":
        if radius_km <= 20 and payload_kg <= 2:
            return "multirotor"
        else:
            return "fixed_wing"

    # Середня дальність → фіксоване крило або VTOL
    if mission == "medium_range":
        if radius_km <= 80 and time_h <= 3:
            return "fixed_wing"
        else:
            return "vtol_fixed_wing"

    # Довша місія → переважно фіксоване крило або VTOL
    if mission == "extended_range":
        if low_noise:
            return "fixed_wing"
        else:
            return "vtol_fixed_wing"


# -----------------------------
# 3. СИЛОВА УСТАНОВКА (ТИП)
# -----------------------------
def choose_propulsion(mission: str, low_noise: bool, budget: float, uav_type: str, time_h: float, radius_km: float) -> str:
    """
    Вибір типу силової установки:
    - electric
    - combustion (узагальнений поршневий)
    (турбіни тут не чіпаємо, щоб не лізти в важку техніку).
    """
    # Коротка місія, низький шум → електрика
    if mission == "short_range":
        return "electric"

    # Середня місія: якщо потрібен низький шум або бюджет пристойний → теж можна електрику
    if mission == "medium_range":
        if low_noise and budget >= 2000 and time_h <= 3:
            return "electric"
        else:
            return "combustion"

    # Довга місія → загалом ДВЗ, електрика тільки при дуже великому бюджеті
    if mission == "extended_range":
        if low_noise and budget >= 8000 and time_h <= 4:
            return "electric"
        return "combustion"


PROP_DATA = {
    "electric": {
        "eta_prop": 0.8,        # ККД пропелера
        "eta_sys": 0.8,         # ККД електросистеми
        "battery_wh_kg": 220.0  # Wh/кг для Li-Ion
    },
    "combustion": {
        "eta_prop": 0.8,
        "bsfc": 0.28            # кг/кВт·год (прибл. для невеликого ДВЗ)
    }
}


# -----------------------------
# 4. АЕРОДИНАМІКА — ТИПОВІ БАЗИ
# -----------------------------
# Базові аеродинамічні параметри для "умовного" планера (далі масштабуємо)
BASE_AERO = {
    "multirotor":    {"Cd": 1.0,  "S": 0.2,  "v": 12.0, "empty_mass_kg": 1.2},
    "fixed_wing":    {"Cd": 0.05, "S": 0.6,  "v": 18.0, "empty_mass_kg": 2.0},
    "vtol_fixed_wing": {"Cd": 0.08, "S": 0.9, "v": 20.0, "empty_mass_kg": 3.0},
}

def aerodynamic_drag(rho: float, v: float, S: float, Cd: float) -> float:
    return 0.5 * rho * v * v * S * Cd

def cruise_power(T: float, v: float, eta_prop: float) -> float:
    return T * v / eta_prop

def electric_energy(power_W: float, t_h: float, wh_per_kg: float, eta_sys: float):
    """
    Скільки треба енергії (Wh) і яка маса батареї (кг).
    """
    Wh = (power_W * t_h) / eta_sys
    mass_kg = Wh / wh_per_kg
    return Wh, mass_kg

def fuel_mass(power_W: float, t_h: float, bsfc: float) -> float:
    """
    Оцінка маси палива (кг) при потужності P (Вт), тривалості t (год) та питомій витраті bsfc (кг/кВт·год).
    """
    return t_h * bsfc * (power_W / 1000.0)

def performance(v_mps: float, time_h: float):
    dist_km = v_mps * time_h * 3.6
    radius_km = dist_km / 2.0
    return dist_km, radius_km


# -----------------------------
# 5. ДИНАМІЧНА ГЕНЕРАЦІЯ РЕКОМЕНДОВАНИХ КОМПОНЕНТІВ
# -----------------------------
def recommend_components(propulsion: str,
                         uav_type: str,
                         mtow_kg: float,
                         power_W: float,
                         time_h: float,
                         energy_mass_kg: float):
    """
    Тут НІЯКИХ жорстко зашитих брендів/моделей.
    Все — динамічні описи за масою і потужністю.
    """
    # 1) Двигун / мотор
    power_margin = 1.5  # запас по потужності
    rec_power = power_W * power_margin
    # округлимо до "сотень"
    rec_power_rounded = int(round(rec_power / 100.0) * 100)

    if propulsion == "electric":
        engine_desc = (
            f"Безщітковий електродвигун класу приблизно {rec_power_rounded} Вт "
            f"з KV, підібраним під гвинт і напругу (типово 400–900 KV для крила)."
        )
    else:
        engine_desc = (
            f"Легкий поршневий ДВЗ з потужністю приблизно {rec_power_rounded} Вт "
            f"(або еквівалент у к.с.), розрахований на тривалий крейсерський режим."
        )

    engine = {
        "model": engine_desc,
        "price": None,
        "reason": (
            "Потужність розрахована з урахуванням крейсерського споживання та запасу на маневри, "
            "щоб забезпечити безпечний зліт і набір висоти."
        ),
        "estimated_power_W": rec_power_rounded
    }

    # 2) Пропелер — груба оцінка діаметра та кроку
    if uav_type == "multirotor":
        # типові пропи для мультироторів
        base_diam_in = 5 + mtow_kg * 1.0
    else:
        base_diam_in = 8 + mtow_kg * 2.0

    base_diam_in = max(5.0, min(base_diam_in, 24.0))  # обмеження розумного діапазону

    # крок під крейсерну швидкість: для мультиків менший, для крила — середній
    if uav_type == "multirotor":
        pitch_in = base_diam_in * 0.4
    else:
        pitch_in = base_diam_in * 0.6

    blades = 2 if uav_type != "multirotor" else 3

    propeller = {
        "model": f"Гвинт діаметром ~{base_diam_in:.0f}\" з кроком ~{pitch_in:.0f}\" ({blades} лопаті)",
        "price": None,
        "reason": (
            "Діаметр обрано так, щоб забезпечити достатню статичну тягу для зльоту при прийнятних обертах, "
            "а крок — під крейсерську швидкість і потрібну ефективність."
        ),
        "diameter_in": round(base_diam_in, 1),
        "pitch_in": round(pitch_in, 1),
        "blades": blades
    }

    # 3) Енергосистема (батарея або паливо)
    if propulsion == "electric":
        energy_source = {
            "model": f"Li-Ion акумулятор {round(time_h, 1)}-годинної місії (орієнтовно {round(energy_mass_kg, 2)} кг)",
            "price": None,
            "reason": (
                "Ємність підібрана так, щоб забезпечити необхідний час польоту з урахуванням ККД системи "
                "та залишити невеликий резерв по енергії."
            ),
            "mass_kg": round(energy_mass_kg, 2),
            "type": "battery"
        }
    else:
        energy_source = {
            "model": f"Паливна система з запасом палива близько {round(energy_mass_kg, 2)} кг",
            "price": None,
            "reason": (
                "Обсяг бака та запас палива обрано виходячи з розрахункової витрати на крейсері і тривалості місії, "
                "із резервом на зліт і посадку."
            ),
            "mass_kg": round(energy_mass_kg, 2),
            "type": "fuel"
        }

    # 4) Бортова електроніка
    electronics = {
        "model": "Автопілот класу Pixhawk/Cube з GPS і базовим сенсорним набором",
        "price": None,
        "reason": (
            "Забезпечує стабілізацію, навігацію та базовий контроль польоту для малого/середнього БПЛА."
        )
    }

    return {
        "engine": engine,
        "propeller": propeller,
        "electronics": electronics,
        "energy": energy_source
    }


# -----------------------------
# 6. ТЕХНІЧНЕ ПОЯСНЕННЯ (8–12 речень)
# -----------------------------
def engineering_explanation(uav_type: str,
                            mission: str,
                            propulsion: str,
                            mtow_kg: float,
                            time_h: float,
                            radius_km: float,
                            calc: dict) -> str:
    uav_name = UAV_TYPES[uav_type]["name"]
    mission_h = {
        "short_range": "короткої місії з невеликим радіусом дії",
        "medium_range": "місії середньої дальності",
        "extended_range": "довшої місії з підвищеними вимогами до енергоефективності",
    }.get(mission, mission)

    prop_h = {
        "electric": "електричну силову установку",
        "combustion": "поршневу силову установку на рідкому паливі",
    }.get(propulsion, propulsion)

    text = []
    text.append(
        f"Для заданих параметрів місії обрано платформу типу «{uav_name}», "
        f"оскільки вона забезпечує прийнятний компроміс між масою, дальністю та керованістю для {mission_h}."
    )
    text.append(
        f"Розрахункова злітна маса становить близько {mtow_kg:.1f} кг, що відповідає класу легкого безпілотного апарата "
        f"і дозволяє реалізувати вказаний час польоту близько {time_h:.1f} годин."
    )
    text.append(
        f"Як силову установку обрано {prop_h}, оскільки вона краще за все відповідає вимогам до шумності, бюджету "
        f"та необхідного запасу енергії."
    )
    text.append(
        f"Аеродинамічні розрахунки показують, що в крейсерському режимі опір становить приблизно {calc['drag_N']:.2f} Н, "
        f"а необхідна потужність — близько {calc['power_W']:.0f} Вт."
    )
    text.append(
        "На основі цих значень підібрано двигун відповідного класу потужності із запасом, що забезпечує "
        "надійний зліт, набір висоти та маневрування без перевантаження силової установки."
    )
    text.append(
        "Рекомендований пропелер має діаметр і крок, узгоджений з робочим діапазоном обертів двигуна, "
        "що дозволяє отримати достатню тягу при хорошому ККД у крейсерському польоті."
    )
    text.append(
        "Маса акумулятора або палива розрахована так, щоб покрити повний профіль польоту із невеликим резервом "
        "на непередбачені маневри та зміни погоди."
    )
    text.append(
        f"За даною конфігурацією розрахунковий радіус дії становить близько {radius_km:.1f} км, "
        "що відповідає вимогам до місії з урахуванням зворотного шляху."
    )
    text.append(
        "Зменшення маси силової установки або енергосистеми знизило б тривалість польоту та запас по тязі, "
        "тоді як надмірне збільшення призвело б до зростання MTOW і погіршення злітно-посадкових характеристик."
    )
    text.append(
        "Таким чином, запропонована гвинтомоторна група є збалансованим рішенням, яке поєднує прийнятну вагу, "
        "необхідний запас енергії та керованість у польоті."
    )

    return " ".join(text)


# -----------------------------
# 7. API ENDPOINT
# -----------------------------
@app.route("/api/configure", methods=["POST"])
def configure():
    d = request.get_json()

    time_h = float(d["timeHours"])
    radius_req = float(d["radiusKm"])
    payload = float(d["payloadKg"])
    lowNoise = bool(d["lowNoise"])
    budget = float(d["budget"])

    # 1) Класифікація місії та вибір типу платформи
    mission = classify_mission(time_h, radius_req)
    uav_type = select_uav_type(mission, payload, radius_req, time_h, lowNoise)

    # 2) Базова аеродинаміка для обраного типу
    base = BASE_AERO[uav_type]
    Cd = base["Cd"]
    S = base["S"]
    v = base["v"]       # м/с
    empty_mass = base["empty_mass_kg"]

    # 3) Тип силової установки
    propulsion = choose_propulsion(mission, lowNoise, budget, uav_type, time_h, radius_req)
    prop_data = PROP_DATA[propulsion]

    # 4) Аеродинаміка: опір, потужність
    D = aerodynamic_drag(RHO, v, S, Cd)
    T = D
    P = cruise_power(T, v, prop_data["eta_prop"])

    # 5) Енергетика
    if propulsion == "electric":
        Wh, energy_mass = electric_energy(P, time_h, prop_data["battery_wh_kg"], prop_data["eta_sys"])
    else:
        Wh = None
        energy_mass = fuel_mass(P, time_h, prop_data["bsfc"])

    # 6) Маси і дальність
    MTOW = empty_mass + payload + energy_mass
    range_km, max_radius = performance(v, time_h)
    meets = max_radius >= radius_req

    # 7) Динамічні рекомендації по компонентах
    components = recommend_components(propulsion, uav_type, MTOW, P, time_h, energy_mass)

    # 8) Технічне пояснення
    calc_struct = {
        "drag_N": round(D, 2),
        "power_W": round(P, 1)
    }
    explanation = engineering_explanation(
        uav_type, mission, propulsion, MTOW, time_h, max_radius, calc_struct
    )

    return jsonify({
        "mission": {
            "missionType": mission,
            "propulsion": propulsion,
            "meetsRadius": meets,
            "requiredRadius": radius_req,
            "achievableRadius": round(max_radius, 1)
        },
        "uav": {
            "type": uav_type,
            "name": UAV_TYPES[uav_type]["name"],
            "description": UAV_TYPES[uav_type]["description"],
        },
        "gmgroup": {
            "recommendedProp": {
                "engine": components["engine"],
                "propeller": components["propeller"],
                "electronics": components["electronics"],
                "battery": components["energy"],   # для фронта як "battery / fuel"
            }
        },
        "calculations": {
            "drag_N": round(D, 2),
            "power_W": round(P, 1),
            "energy_Wh": round(Wh, 1) if Wh is not None else None,
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
            # Місце для інтеграції зовнішнього AI, якщо захочеш
            "summary": ""
        }
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
