FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 5000
# --workers 1: issues スナップショットのキャッシュとバックグラウンド更新スレッドを
#   単一プロセスに集約する（複数ワーカーだとキャッシュが分裂し二重取得になるため）。
#   同時接続は --threads で捌く。--timeout はコールドスタート時の同期取得に余裕を持たせる。
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "1", "--threads", "4", "--timeout", "60", "app:app"]
