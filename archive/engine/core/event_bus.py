
class EventBus:
    def __init__(self):
        self.subscribers = []

    def subscribe(self, fn):
        self.subscribers.append(fn)

    def publish(self, event):
        for fn in self.subscribers:
            fn(event)
