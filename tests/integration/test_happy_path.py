from conftest import DEMO_USER

ORDER = {"item": "book", "quantity": 2, "amount_cents": 1500}


def test_health(client):
    assert client.get("/nginx-health").status_code == 200
    assert client.get("/health").status_code == 200

    ready = client.get("/ready")
    assert ready.status_code == 200
    assert ready.json()["checks"] == {"auth": "ok", "order": "ok"}


def test_login_returns_token(client):
    response = client.post("/login", json=DEMO_USER)

    assert response.status_code == 200
    body = response.json()
    assert body["access_token"]
    assert body["token_type"] == "bearer"
    assert body["expires_in"] > 0


def test_login_bad_credentials(client):
    response = client.post("/login", json={"username": "alice", "password": "wrong"})
    assert response.status_code == 401


def test_create_order_happy_path(client, token):
    headers = {"Authorization": f"Bearer {token}"}

    created = client.post("/orders", json=ORDER, headers=headers)
    assert created.status_code == 201, created.text
    order = created.json()
    assert order["status"] == "paid"
    assert order["payment_id"]
    assert order["user_id"] == DEMO_USER["username"]
    assert order["item"] == ORDER["item"]
    assert order["quantity"] == ORDER["quantity"]
    assert order["amount_cents"] == ORDER["amount_cents"]

    # Read back through the full stack to confirm the order was persisted in PostgreSQL.
    fetched = client.get(f"/orders/{order['id']}", headers=headers)
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["status"] == "paid"
    assert fetched.json()["payment_id"] == order["payment_id"]


def test_create_order_without_token(client):
    assert client.post("/orders", json=ORDER).status_code == 401


def test_create_order_with_invalid_token(client):
    response = client.post("/orders", json=ORDER, headers={"Authorization": "Bearer not-a-token"})
    assert response.status_code == 401
