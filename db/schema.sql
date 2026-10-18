-- Products table: stores items available for purchase.
-- Enforces price_paise > 0 and stock >= 0 so stock can never go negative.
CREATE TABLE IF NOT EXISTS products (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(255) NOT NULL,
    price_paise INT NOT NULL CHECK (price_paise > 0),
    stock INT NOT NULL CHECK (stock >= 0)
);

-- Orders table: tracks orders placed by authenticated users.
-- Enforces allowed status transitions ('created', 'paid', 'failed'), non-negative total,
-- a unique idempotency key per user, and a unique nullable razorpay_order_id.
CREATE TABLE IF NOT EXISTS orders (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id INTEGER NOT NULL,
    status VARCHAR(20) NOT NULL CHECK (status IN ('created', 'paid', 'failed')),
    total_paise INT NOT NULL CHECK (total_paise >= 0),
    razorpay_order_id VARCHAR(255) UNIQUE,
    razorpay_payment_id VARCHAR(255),
    idempotency_key VARCHAR(255),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT unique_user_idempotency UNIQUE (user_id, idempotency_key)
);

-- Order items table: stores snapshot of items in an order.
-- quantity must be positive, and unit_price_paise is copied from products at order time
-- to preserve historical price accuracy even if catalog price changes later.
CREATE TABLE IF NOT EXISTS order_items (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    order_id UUID NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    product_id UUID NOT NULL REFERENCES products(id),
    quantity INT NOT NULL CHECK (quantity > 0),
    unit_price_paise INT NOT NULL CHECK (unit_price_paise > 0)
);

-- Webhook events table: records handled webhook event IDs from payment gateway.
-- Primary key on event_id guarantees duplicate deliveries are safely ignored.
CREATE TABLE IF NOT EXISTS webhook_events (
    event_id VARCHAR(255) PRIMARY KEY,
    event_type VARCHAR(100) NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
