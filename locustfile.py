import time
from locust import HttpUser, task, between, User, TaskSet, events
from websocket import create_connection

class QuickstartUser(HttpUser):
    # note: use ws:// for testing, wss requires ssl
    wsurl = 'wss://example.com/ws/lots' # no trailing /
    wait_time = between(1, 10)
    lot_to_bid_on = 1234

    @task
    def lot_list(self):
        self.client.get("/lots")

    @task(3)
    def view_lot(self):
        for lot in range(1110, 1115):
            ws = create_connection(f"{wsurl}/{lot}/")
            self.client.get(f"/lots/{self.lot}", name="/lot")
            time.sleep(1)
            ws.close()
    
    @task(10)
    def bid(self):
        self.client.post("/login", json={"username":"tester", "password":"1234"})
        ws = create_connection(f"{self.wsurl}/{lot}/")
        ws.send('{"bid":10}')
        result = ws.recv()
        ws.close()

    def on_start(self):
        pass
