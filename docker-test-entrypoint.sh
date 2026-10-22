#!/bin/sh
set -e

# Safely construct an isolated test .env strictly for pytest / conftest.py safeguards.
# This prevents using production Neon credentials or real payment secrets in Docker tests.
cat <<EOF > /app/.env
DATABASE_URL=${DATABASE_URL:-postgresql://cartapi_user:cartapi_password@db:5432/cartapi}
TEST_DATABASE_URL=${TEST_DATABASE_URL:-postgresql://cartapi_test_user:cartapi_test_password@db_test:5432/cartapi_test}
JWT_SECRET=${JWT_SECRET:-dev_jwt_secret_cartapi_test_isolation_32chars}
JWT_ALGORITHM=${JWT_ALGORITHM:-HS256}
RAZORPAY_KEY_ID=${RAZORPAY_KEY_ID:-rzp_test_placeholder_key_id}
RAZORPAY_KEY_SECRET=${RAZORPAY_KEY_SECRET:-mock_key_secret_for_testing}
RAZORPAY_WEBHOOK_SECRET=${RAZORPAY_WEBHOOK_SECRET:-mock_webhook_secret_for_signature_testing}
DEMO_MODE=${DEMO_MODE:-false}
EOF

# Ensure test database is reachable when running pytest or python
if [ "$1" = "pytest" ] || [ "$1" = "python" ]; then
  python -c "
import os, sys, time, psycopg
db_url = os.environ.get('TEST_DATABASE_URL', 'postgresql://cartapi_test_user:cartapi_test_password@db_test:5432/cartapi_test')
max_retries = 30
for i in range(max_retries):
    try:
        with psycopg.connect(db_url, connect_timeout=3) as conn:
            print('Connected to test database.')
            sys.exit(0)
    except Exception as e:
        if i < max_retries - 1:
            time.sleep(1)
        else:
            print(f'ERROR: Could not connect to test database after {max_retries}s: {e}', file=sys.stderr)
            sys.exit(1)
"
fi

exec "$@"
