from PyQt6.QtCore import QObject, pyqtSignal
from enum import Enum

class State(Enum):
    IDLE = "IDLE"
    LISTENING = "LISTENING"
    PROCESSING = "PROCESSING"
    EXECUTING = "EXECUTING"
    RESPONDING = "RESPONDING"
    ERROR = "ERROR"

class StateMachine(QObject):
    state_changed = pyqtSignal(State, State)
    
    VALID_TRANSITIONS = {
        State.IDLE: [State.LISTENING, State.ERROR],
        State.LISTENING: [State.PROCESSING, State.ERROR, State.IDLE],
        State.PROCESSING: [State.EXECUTING, State.RESPONDING, State.ERROR, State.IDLE],
        State.EXECUTING: [State.RESPONDING, State.ERROR, State.IDLE],
        State.RESPONDING: [State.IDLE, State.ERROR],
        State.ERROR: [State.IDLE],
    }
    
    def __init__(self):
        super().__init__()
        self._state = State.IDLE
    
    @property
    def state(self):
        return self._state
    
    def can_transition(self, new_state: State) -> bool:
        return new_state in self.VALID_TRANSITIONS.get(self._state, [])
    
    def transition(self, new_state: State):
        if self.can_transition(new_state):
            old_state = self._state
            self._state = new_state
            self.state_changed.emit(old_state, new_state)
            return True
        return False
    
    def start_listening(self):
        return self.transition(State.LISTENING)
    
    def start_processing(self):
        return self.transition(State.PROCESSING)
    
    def start_executing(self):
        return self.transition(State.EXECUTING)
    
    def start_responding(self):
        return self.transition(State.RESPONDING)
    
    def go_idle(self):
        return self.transition(State.IDLE)
    
    def go_error(self):
        return self.transition(State.ERROR)
