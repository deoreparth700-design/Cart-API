// ─── CartAPI Storefront — app.js ─────────────────────────────────────────────
// Vanilla JS SPA: fetches a demo JWT, loads products, manages a cart,
// submits orders, and opens Razorpay Checkout.
// No external libraries. No secrets ever touch this file.
// ─────────────────────────────────────────────────────────────────────────────

// ── Emoji icons per product name (fuzzy match) ────────────────────────────────
const PRODUCT_ICONS = {
  notebook: '📓', pen: '🖊️', pencil: '✏️', eraser: '🔲',
  ruler: '📏', book: '📚', paper: '📄', stapler: '📎',
  folder: '📁', tape: '🔖', scissors: '✂️', marker: '🖍️',
};
function iconFor(name) {
  const lower = name.toLowerCase();
  for (const [key, emoji] of Object.entries(PRODUCT_ICONS)) {
    if (lower.includes(key)) return emoji;
  }
  return '📦';
}

// ── Format paise → ₹ string ───────────────────────────────────────────────────
function formatRupees(paise) {
  return '₹' + (paise / 100).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

// ── Generate a UUIDv4 for idempotency keys ────────────────────────────────────
function uuid4() {
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
    const r = Math.random() * 16 | 0;
    return (c === 'x' ? r : (r & 0x3 | 0x8)).toString(16);
  });
}

// ── Application State ─────────────────────────────────────────────────────────
const state = {
  token: null,          // Demo JWT string
  products: [],         // [{id, name, price_paise, stock}]
  cart: new Map(),      // productId → {product, qty}
};

// ── Toast notification ────────────────────────────────────────────────────────
function toast(message, type = 'info', duration = 3000) {
  const container = document.getElementById('toast-container');
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  el.textContent = message;
  container.appendChild(el);
  setTimeout(() => {
    el.classList.add('out');
    setTimeout(() => el.remove(), 350);
  }, duration);
}

// ── API helpers ───────────────────────────────────────────────────────────────
async function apiFetch(path, options = {}) {
  // Destructure headers out so the spread below doesn't overwrite them.
  const { headers: extraHeaders = {}, ...restOptions } = options;
  const res = await fetch(path, {
    ...restOptions,
    headers: { 'Content-Type': 'application/json', ...extraHeaders },
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw Object.assign(new Error(body.detail || `HTTP ${res.status}`), { status: res.status });
  }
  return res.json();
}

// ── Step 1: Fetch demo token ──────────────────────────────────────────────────
async function fetchDemoToken() {
  try {
    const data = await apiFetch('/api/demo-token');
    state.token = data.token;
  } catch (e) {
    if (e.status === 404) {
      toast('Demo mode is disabled. Store is in preview mode.', 'info', 5000);
    } else {
      toast('Could not obtain demo token — some features disabled', 'error', 6000);
    }
  }
}

// ── Step 2: Fetch and render product catalog ──────────────────────────────────
async function fetchProducts() {
  const grid = document.getElementById('product-grid');
  // Show 4 skeleton cards while loading
  grid.innerHTML = Array(4).fill('<div class="skeleton"></div>').join('');

  try {
    state.products = await apiFetch('/api/products');
  } catch (e) {
    grid.innerHTML = `<p style="color:var(--error);grid-column:1/-1">Failed to load products. Is the server running?</p>`;
    toast('Failed to load products', 'error');
    return;
  }

  document.getElementById('product-count').textContent =
    `${state.products.length} item${state.products.length !== 1 ? 's' : ''}`;

  if (!state.products.length) {
    grid.innerHTML = '<p style="color:var(--text-muted);grid-column:1/-1">No products in catalog. Run db/seed.sql.</p>';
    return;
  }

  grid.innerHTML = '';
  state.products.forEach((p, i) => {
    const card = document.createElement('div');
    card.className = 'product-card';
    card.style.animationDelay = `${i * 60}ms`;

    const stockLabel = p.stock === 0
      ? '<span class="product-stock out">⚠ Out of stock</span>'
      : p.stock < 5
        ? `<span class="product-stock low">⚡ Only ${p.stock} left</span>`
        : `<span class="product-stock">✓ ${p.stock} in stock</span>`;

    card.innerHTML = `
      <div class="product-icon">${iconFor(p.name)}</div>
      <div class="product-name">${p.name}</div>
      <div class="product-price gradient-text">${formatRupees(p.price_paise)}</div>
      ${stockLabel}
      <div class="card-footer">
        <div class="qty-control">
          <button class="qty-btn" id="qty-dec-${p.id}" onclick="adjustQty('${p.id}', -1)" ${p.stock === 0 ? 'disabled' : ''}>−</button>
          <span class="qty-display" id="qty-val-${p.id}">1</span>
          <button class="qty-btn" id="qty-inc-${p.id}" onclick="adjustQty('${p.id}', 1)" ${p.stock === 0 ? 'disabled' : ''}>+</button>
        </div>
        <button class="btn btn-primary btn-sm add-btn" id="add-${p.id}"
          onclick="addToCart('${p.id}')" ${p.stock === 0 ? 'disabled' : ''}>
          Add
        </button>
      </div>
    `;
    grid.appendChild(card);
  });
}

// ── Quantity selector on product card ─────────────────────────────────────────
// Tracks qty state per product in a simple object before adding to cart
const productQtyMap = {};   // productId → current selected qty on the card

function adjustQty(productId, delta) {
  const product = state.products.find(p => p.id === productId);
  if (!product) return;
  const current = productQtyMap[productId] || 1;
  const next = Math.max(1, Math.min(product.stock, current + delta));
  productQtyMap[productId] = next;
  const el = document.getElementById(`qty-val-${productId}`);
  if (el) el.textContent = next;
}

// ── Cart management ───────────────────────────────────────────────────────────
function addToCart(productId) {
  const product = state.products.find(p => p.id === productId);
  if (!product || product.stock === 0) return;

  const qtyToAdd = productQtyMap[productId] || 1;
  const existing = state.cart.get(productId);
  const newQty = existing ? Math.min(product.stock, existing.qty + qtyToAdd) : qtyToAdd;

  state.cart.set(productId, { product, qty: newQty });

  // Bump cart button
  const cartBtn = document.getElementById('cart-btn');
  cartBtn.classList.remove('bump');
  void cartBtn.offsetWidth;   // force reflow to restart animation
  cartBtn.classList.add('bump');

  toast(`${product.name} added to cart`, 'success', 2000);
  updateCartUI();
}

function removeFromCart(productId) {
  state.cart.delete(productId);
  updateCartUI();
}

function cartTotal() {
  let total = 0;
  for (const { product, qty } of state.cart.values()) {
    total += product.price_paise * qty;
  }
  return total;
}

function cartItemCount() {
  let count = 0;
  for (const { qty } of state.cart.values()) count += qty;
  return count;
}

// ── Render cart sidebar ───────────────────────────────────────────────────────
function updateCartUI() {
  const count = cartItemCount();
  const countEl = document.getElementById('cart-count');

  // Update badge
  if (count > 0) {
    countEl.textContent = count;
    countEl.classList.remove('hidden');
  } else {
    countEl.classList.add('hidden');
  }

  // Update checkout button state
  document.getElementById('checkout-btn').disabled = count === 0;

  // Render items
  const itemsEl = document.getElementById('cart-items');
  const totalEl = document.getElementById('cart-total');

  if (count === 0) {
    itemsEl.innerHTML = `
      <div class="cart-empty">
        <div class="cart-empty-icon">🛒</div>
        <p>Your cart is empty.<br>Add some items from the store.</p>
      </div>`;
    totalEl.textContent = '₹0.00';
    return;
  }

  itemsEl.innerHTML = '';
  for (const [productId, { product, qty }] of state.cart) {
    const subtotal = product.price_paise * qty;
    const item = document.createElement('div');
    item.className = 'cart-item';
    item.innerHTML = `
      <div class="cart-item-icon">${iconFor(product.name)}</div>
      <div class="cart-item-info">
        <div class="cart-item-name">${product.name}</div>
        <div class="cart-item-meta">${formatRupees(product.price_paise)} × ${qty}</div>
      </div>
      <div class="cart-item-subtotal gradient-text">${formatRupees(subtotal)}</div>
      <button class="cart-item-remove" onclick="removeFromCart('${productId}')" title="Remove">✕</button>
    `;
    itemsEl.appendChild(item);
  }

  totalEl.textContent = formatRupees(cartTotal());
}

// ── Cart drawer open/close ────────────────────────────────────────────────────
function openCart() {
  document.getElementById('cart-overlay').classList.add('open');
  document.getElementById('cart-drawer').classList.add('open');
  document.body.style.overflow = 'hidden';
}
function closeCart() {
  document.getElementById('cart-overlay').classList.remove('open');
  document.getElementById('cart-drawer').classList.remove('open');
  document.body.style.overflow = '';
}

// ── Modal helpers ─────────────────────────────────────────────────────────────
function openModal() {
  document.getElementById('modal-overlay').classList.add('open');
  document.body.style.overflow = 'hidden';
}
function closeModal() {
  document.getElementById('modal-overlay').classList.remove('open');
  document.body.style.overflow = '';
}

function setModalState(state_) {
  const modal = document.getElementById('modal');
  const icon  = document.getElementById('modal-icon');
  const title = document.getElementById('modal-title');
  const body  = document.getElementById('modal-body');
  const spinner = document.getElementById('modal-spinner');
  const actions = document.getElementById('modal-actions');

  modal.className = '';
  spinner.style.display = 'none';
  actions.innerHTML = '';

  switch (state_) {
    case 'placing': {
      spinner.style.display = 'block';
      icon.textContent = '';
      title.textContent = 'Placing your order…';
      body.textContent = 'Talking to Razorpay. Please wait.';
      break;
    }
    case 'pay': {
      icon.textContent = '💳';
      title.textContent = 'Ready to Pay';
      body.textContent = 'Your order is confirmed. Razorpay Checkout will open now.';
      const btn = document.createElement('button');
      btn.className = 'btn btn-ghost';
      btn.textContent = 'Close';
      btn.onclick = closeModal;
      actions.appendChild(btn);
      break;
    }
    case 'success': {
      modal.className = 'modal-success';
      icon.textContent = '🎉';
      title.textContent = 'Payment Submitted!';
      body.textContent = 'Razorpay has received your payment. The webhook will confirm and update your order status.';
      const btn = document.createElement('button');
      btn.className = 'btn btn-primary';
      btn.textContent = 'Continue Shopping';
      btn.onclick = () => { closeModal(); state.cart.clear(); updateCartUI(); closeCart(); fetchProducts(); };
      actions.appendChild(btn);
      break;
    }
    case 'error': {
      modal.className = 'modal-error';
      icon.textContent = '⚠️';
      title.textContent = 'Order Failed';
      body.textContent = currentError || 'Something went wrong. Your cart has been preserved.';
      const btn = document.createElement('button');
      btn.className = 'btn btn-ghost';
      btn.textContent = 'Try Again';
      btn.onclick = closeModal;
      actions.appendChild(btn);
      break;
    }
  }
}

let currentError = '';

// ── Checkout flow ─────────────────────────────────────────────────────────────
async function checkout() {
  if (!state.token) {
    toast('Demo checkout is disabled on this server.', 'info', 4000);
    return;
  }
  if (state.cart.size === 0) {
    toast('Cart is empty', 'error');
    return;
  }

  closeCart();
  openModal();
  setModalState('placing');

  // Build request body from cart
  const items = [];
  for (const [productId, { qty }] of state.cart) {
    items.push({ product_id: productId, quantity: qty });
  }

  const idempotencyKey = uuid4();

  let order;
  try {
    order = await apiFetch('/api/orders', {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${state.token}`,
        'Idempotency-Key': idempotencyKey,
      },
      body: JSON.stringify({ items }),
    });
  } catch (e) {
    currentError = e.message;
    setModalState('error');
    toast(e.message, 'error', 5000);
    return;
  }

  // Show the order summary card in the modal
  const body = document.getElementById('modal-body');
  body.innerHTML = `
    <div class="order-summary-box">
      <div>Order ID: <strong>${order.id.slice(0,8)}…</strong></div>
      <div>Total: <strong class="gradient-text">${formatRupees(order.total_paise)}</strong></div>
      <div style="margin-top:8px;font-size:0.78rem;color:var(--text-muted)">
        Razorpay Checkout opening…
      </div>
    </div>
    Your cart is reserved. Complete payment in the Razorpay window.
  `;
  setModalState('pay');

  // Open Razorpay Checkout
  const rzp = new Razorpay({
    key: order.razorpay_key_id || order.RAZORPAY_KEY_ID,
    amount: order.total_paise,
    currency: 'INR',
    name: 'CartAPI Store',
    description: `Order ${order.id.slice(0,8)}`,
    order_id: order.razorpay_order_id,
    handler: function (response) {
      // Payment submitted — webhook will do the actual status update
      setModalState('success');
    },
    prefill: { name: 'Demo User', email: 'demo@cartapi.dev' },
    theme: { color: '#6366f1' },
    modal: {
      ondismiss: function () {
        // User closed Razorpay without paying — keep modal open with "Ready to Pay"
        toast('Payment dismissed. Your order is still saved — you can retry.', 'info', 5000);
      },
    },
  });

  rzp.on('payment.failed', function (response) {
    currentError = response.error.description || 'Payment failed';
    setModalState('error');
    toast('Payment failed: ' + currentError, 'error', 6000);
  });

  rzp.open();
}

// ── Bootstrap ─────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', async () => {
  // Wire up static UI events
  document.getElementById('cart-btn').addEventListener('click', openCart);
  document.getElementById('cart-close').addEventListener('click', closeCart);
  document.getElementById('cart-overlay').addEventListener('click', closeCart);
  document.getElementById('checkout-btn').addEventListener('click', checkout);

  // Close modal on overlay click
  document.getElementById('modal-overlay').addEventListener('click', (e) => {
    if (e.target === e.currentTarget) closeModal();
  });

  // Initialise
  await fetchDemoToken();
  await fetchProducts();
  updateCartUI();
});

// Expose functions used by inline onclick handlers on dynamically created elements
window.addToCart    = addToCart;
window.removeFromCart = removeFromCart;
window.adjustQty    = adjustQty;
