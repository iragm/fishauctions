import time
from locust import HttpUser, task, between, User, TaskSet, events
from websocket import create_connection

class QuickstartUser(HttpUser):
    # note: use ws:// for testing, wss requires ssl
    wsurl = 'wss://example.com/ws/lots' # no trailing /
    wait_time = between(1, 10)
    lot_to_bid_on = 1234
    lot_start_pk = 1110
    lot_end_pk = 1115

    @task
    def lot_list(self):
        self.client.get("/lots")

    @task(3)
    def view_lot(self):
        for lot in range(self.lot_start_pk, self.lot_end_pk):
            #ws = create_connection(f"{self.wsurl}/{lot}/")
            self.client.get(f"/lots/{lot}", name="/lot")
            time.sleep(1)
            #ws.close()
    
    # @task(10)
    # def bid(self):
    #     self.client.post("/login", json={"username":"tester", "password":"1234"})
    #     ws = create_connection(f"{self.wsurl}/{self.lot_to_bid_on}/")
    #     ws.send('{"bid":10}')
    #     result = ws.recv()
    #     ws.close()

    def on_start(self):
        pass
