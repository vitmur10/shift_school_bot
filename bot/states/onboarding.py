"""
FSM-стани aiogram (StatesGroup) для онбордингу.

Потік станів:

  /start
    │
    ▼
  identify_participant() -- автоматично, без участі FSM, якщо знайдено
    │
    ├── знайдено за tg_id/username -> одразу показуємо доступ (без FSM)
    │
    └── не знайдено -> переходимо в FSM:
          │
          ▼
        WAITING_FOR_PHONE  -- запит контакту (кнопка "Поділитись номером")
          │
          ├── телефон збігся з кимось у Sheets -> прив'язка, ВИХІД з FSM
          │
          └── телефон НЕ збігся -> WAITING_FOR_TOKEN
                │
                ▼
              WAITING_FOR_TOKEN  -- запит токена текстом
                │
                ├── токен валідний -> прив'язка, ВИХІД з FSM
                └── токен невалідний -> лишаємось у WAITING_FOR_TOKEN,
                                         просимо спробувати ще раз

fsm_state з Participant (Google Sheets) використовується ЛИШЕ для
відновлення стану після рестарту бота (aiogram MemoryStorage губиться
при перезапуску процесу) — детальніше див. bot/middlewares/cache_middleware.py.
"""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class OnboardingStates(StatesGroup):
    waiting_for_phone = State()
    waiting_for_token = State()


# рядкові значення для серіалізації в Participant.fsm_state / колонку Sheets —
# aiogram State.state вже є рядком виду "OnboardingStates:waiting_for_phone",
# тому окремий мапінг не потрібен, але явні константи зручніші для читання
# коду в access_control/handlers, ніж порівняння рядків напряму.
STATE_WAITING_FOR_PHONE = OnboardingStates.waiting_for_phone.state
STATE_WAITING_FOR_TOKEN = OnboardingStates.waiting_for_token.state