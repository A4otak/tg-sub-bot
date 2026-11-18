from aiogram.fsm.state import State, StatesGroup


class AdminStates(StatesGroup):
    grant = State()
    revoke = State()
    broadcast = State()
