class MockBroker:

    def __init__(self):
        self.is_paper = True
        self.price = 23550

    def start_feed(self, symbols):
        pass

    def ltp(self, symbol):
        return self.price

    def get_atm_option(self, side):
        return "NIFTY_FAKE_OPT", 50