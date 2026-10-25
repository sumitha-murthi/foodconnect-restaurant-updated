# FoodConnect Kitchen – Restaurant Portal

Flask app: receives order CSVs, shows them, lets staff accept/reject each item, generates the result CSV and posts it back to the customer API.

## Run locally
    pip install -r requirements.txt
    python app.py                       # http://127.0.0.1:5001
The result is posted to the `callback_url` the customer app sends with each upload, so no extra config is needed locally.

## Environment variables
| Name | Purpose |
|---|---|
| `API_KEY` | Shared secret expected in `X-API-Key`; must match the customer portal |
| `CUSTOMER_API_URL` | Optional. Customer portal base URL; if empty, the upload's `callback_url` is used |

## Deploy on Render (free)
1. Push this folder to its own GitHub repo.
2. Render → New → Web Service → pick the repo. Runtime: Python, Instance type: Free.
3. Build command: `pip install -r requirements.txt`
4. Start command: `gunicorn app:app --workers 1 --timeout 120`
5. Add `API_KEY` (same value as the customer portal), then deploy.

## API
- `POST /api/orders/upload` – multipart `file` (.csv), optional `callback_url`; header `X-API-Key`
- `GET /api/orders` · `GET /api/orders/<id>` · `GET /api/orders/<id>/input` · `GET /api/orders/<id>/result`
- `POST /api/orders/<id>/process` – JSON `{decisions:{"Item":"ACCEPTED"|"REJECTED"}}` → writes result CSV and sends it
- `POST /api/orders/result` – JSON `{order_id}` → re-sends a processed order's result CSV

Required input columns: `order_id, customer_id, restaurant_id, item, quantity, unit_price`.
Free-tier note: the filesystem is wiped on restart/redeploy/idle spin-down.
