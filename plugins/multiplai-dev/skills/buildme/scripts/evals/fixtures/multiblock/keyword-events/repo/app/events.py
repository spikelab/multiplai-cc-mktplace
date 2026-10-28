"""In-process event bus. Handlers register with `on` and run on `emit`."""

_handlers: dict[str, list] = {}


def on(name: str):
    def register(fn):
        _handlers.setdefault(name, []).append(fn)
        return fn

    return register


def emit(name: str, payload: dict) -> list:
    return [fn(**payload) for fn in _handlers.get(name, [])]
