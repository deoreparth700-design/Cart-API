-- Seed sample products with prices in paise (₹1 = 100 paise) and initial inventory stock.
INSERT INTO products (name, price_paise, stock) VALUES
    ('Notebook', 15000, 50),
    ('Pen', 2000, 100),
    ('Pencil', 1000, 75),
    ('Eraser', 500, 60),
    ('Ruler', 3000, 40)
ON CONFLICT DO NOTHING;
