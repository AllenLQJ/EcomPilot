-- EcomPilot MySQL 8.0 schema (SSOT: C 端商品/订单/优惠券/退款执行). 金额一律整型分(*_cents).
SET NAMES utf8mb4;

CREATE TABLE catalog_categories (
  catalog_category_id VARCHAR(64) PRIMARY KEY,
  parent_id VARCHAR(64) NULL,
  category_name VARCHAR(64) NOT NULL,
  item_type ENUM('PHYSICAL','SERVICE') NOT NULL DEFAULT 'PHYSICAL'
) ENGINE=InnoDB;

CREATE TABLE products_sku (
  sku_id VARCHAR(64) PRIMARY KEY,
  tenant_id VARCHAR(64) NOT NULL,
  shop_id VARCHAR(64) NOT NULL,
  catalog_category_id VARCHAR(64) NOT NULL,
  sku_name VARCHAR(128) NOT NULL,
  brand_name VARCHAR(64) NULL,
  unit_price_cents BIGINT NOT NULL,
  sellable_stock INT NOT NULL DEFAULT 0,
  defective_stock INT NOT NULL DEFAULT 0,
  version INT NOT NULL DEFAULT 0,
  positive_review_ratio DECIMAL(4,3) NOT NULL DEFAULT 0.950,
  is_addon_pool TINYINT(1) NOT NULL DEFAULT 0,
  image_url VARCHAR(255) NULL,
  KEY idx_sku_cat (catalog_category_id),
  KEY idx_sku_tenant (tenant_id),
  CONSTRAINT fk_sku_cat FOREIGN KEY (catalog_category_id) REFERENCES catalog_categories(catalog_category_id),
  CONSTRAINT chk_stock CHECK (sellable_stock >= 0 AND defective_stock >= 0 AND unit_price_cents >= 0)
) ENGINE=InnoDB;

-- 跨店满减档位(按 tenant 汇总各店铺参与金额后匹配最高一档)
CREATE TABLE promo_rules (
  rule_id VARCHAR(64) PRIMARY KEY,
  tenant_id VARCHAR(64) NOT NULL,
  threshold_cents BIGINT NOT NULL,
  discount_cents BIGINT NOT NULL,
  is_active TINYINT(1) NOT NULL DEFAULT 1,
  KEY idx_promo_tenant (tenant_id)
) ENGINE=InnoDB;

CREATE TABLE user_coupons (
  user_coupon_id VARCHAR(64) PRIMARY KEY,
  tenant_id VARCHAR(64) NOT NULL,
  user_id VARCHAR(64) NOT NULL,
  coupon_name VARCHAR(64) NOT NULL,
  catalog_category_id VARCHAR(64) NULL,          -- NULL 表示全品类券
  threshold_cents BIGINT NOT NULL,
  discount_cents BIGINT NOT NULL,
  status ENUM('UNUSED','LOCKED','USED') NOT NULL DEFAULT 'UNUSED',
  order_id VARCHAR(64) NULL,
  expires_at DATETIME NOT NULL,
  KEY idx_cpn_user (tenant_id, user_id, status),
  KEY idx_cpn_order (order_id)
) ENGINE=InnoDB;

CREATE TABLE price_quotes (
  quote_id VARCHAR(64) PRIMARY KEY,
  tenant_id VARCHAR(64) NOT NULL,
  user_id VARCHAR(64) NOT NULL,
  items_json JSON NOT NULL,                       -- [{sku_id, qty}]
  coupon_ids_json JSON NOT NULL,                  -- [user_coupon_id]
  total_original_cents BIGINT NOT NULL,
  total_discount_cents BIGINT NOT NULL,
  total_payable_cents BIGINT NOT NULL,
  quote_snapshot_json JSON NOT NULL,              -- 价格明细与分摊, 供二次核价对比
  suggested_addons_json JSON NULL,
  created_at DATETIME NOT NULL,
  expires_at DATETIME NOT NULL,
  KEY idx_quote_user (tenant_id, user_id)
) ENGINE=InnoDB;

CREATE TABLE orders (
  order_id VARCHAR(64) PRIMARY KEY,
  idempotency_key VARCHAR(128) NOT NULL,
  quote_id VARCHAR(64) NOT NULL,
  tenant_id VARCHAR(64) NOT NULL,
  user_id VARCHAR(64) NOT NULL,
  total_original_cents BIGINT NOT NULL,
  total_discount_cents BIGINT NOT NULL,
  total_payable_cents BIGINT NOT NULL,
  refunded_cents BIGINT NOT NULL DEFAULT 0,
  order_status ENUM('CREATED','CLOSED_OUT_OF_STOCK','STOCK_LOCKED','PAID_SIMULATED','FULFILLED','AFTER_SALE_OPEN','REFUNDING','REFUNDED_PARTIAL','REFUNDED_FULL','CANCELLED') NOT NULL,
  created_at DATETIME NOT NULL,
  paid_at DATETIME NULL,
  fulfilled_at DATETIME NULL,
  cancelled_at DATETIME NULL,
  pay_deadline_at DATETIME NOT NULL,
  UNIQUE KEY uk_order_idem (tenant_id, user_id, idempotency_key),
  KEY idx_order_user (tenant_id, user_id, order_status)
) ENGINE=InnoDB;

CREATE TABLE order_items (
  order_item_id VARCHAR(64) PRIMARY KEY,
  order_id VARCHAR(64) NOT NULL,
  sku_id VARCHAR(64) NOT NULL,
  shop_id VARCHAR(64) NOT NULL,
  catalog_category_id VARCHAR(64) NOT NULL,
  sku_name VARCHAR(128) NOT NULL,
  qty INT NOT NULL,
  unit_price_cents BIGINT NOT NULL,
  original_cents BIGINT NOT NULL,
  discount_share_cents BIGINT NOT NULL,
  payable_cents BIGINT NOT NULL,
  refunded_cents BIGINT NOT NULL DEFAULT 0,
  KEY idx_item_order (order_id),
  CONSTRAINT fk_item_order FOREIGN KEY (order_id) REFERENCES orders(order_id)
) ENGINE=InnoDB;

CREATE TABLE refund_executions (
  refund_execution_id VARCHAR(64) PRIMARY KEY,
  refund_idempotency_key VARCHAR(128) NOT NULL,
  claim_no VARCHAR(64) NOT NULL,
  order_id VARCHAR(64) NOT NULL,
  order_item_id VARCHAR(64) NULL,
  refunded_cents BIGINT NOT NULL,
  restock_policy ENUM('SELLABLE_WAREHOUSE','DEFECTIVE_WAREHOUSE','NO_RESTOCK') NOT NULL,
  execution_status VARCHAR(16) NOT NULL,          -- SUCCESS | FAILED
  order_status_after VARCHAR(32) NOT NULL,
  approval_mode VARCHAR(32) NULL,
  supervisor_user_id VARCHAR(64) NULL,
  executed_at DATETIME NOT NULL,
  UNIQUE KEY uk_refund_key (refund_idempotency_key),
  KEY idx_refund_order (order_id)
) ENGINE=InnoDB;

-- C 端侧售后申请(用户二次确认后才写入; 与 Ops 的 claim_tickets 通过 claim_no 对应)
CREATE TABLE aftersale_requests (
  claim_no VARCHAR(64) PRIMARY KEY,
  tenant_id VARCHAR(64) NOT NULL,
  user_id VARCHAR(64) NOT NULL,
  order_id VARCHAR(64) NOT NULL,
  order_item_id VARCHAR(64) NOT NULL,
  claim_status VARCHAR(32) NOT NULL,              -- SUBMITTED | SUBMIT_FAILED | WITHDRAWN | COMPLETED
  buyer_reason_text TEXT NULL,
  result_reason VARCHAR(200) NULL,                -- EcomOps 回写的驳回原因（REJECTED 时有值）；已有库由 pilot.core.migrations 幂等补列
  created_at DATETIME NOT NULL,
  UNIQUE KEY uk_claim_item (order_id, order_item_id),
  KEY idx_claim_user (tenant_id, user_id)
) ENGINE=InnoDB;

CREATE TABLE user_session_memory (
  session_id VARCHAR(64) NOT NULL,
  tenant_id VARCHAR(64) NOT NULL,
  user_id VARCHAR(64) NOT NULL,
  messages_json JSON NOT NULL,
  context_json JSON NULL,
  updated_at DATETIME NOT NULL,
  PRIMARY KEY (tenant_id, user_id, session_id),
  KEY idx_session (session_id)
) ENGINE=InnoDB;

CREATE TABLE nlu_label_to_catalog_map (
  nlu_label VARCHAR(32) PRIMARY KEY,
  label_kind ENUM('CATEGORY','BRAND','SERVICE') NOT NULL,
  catalog_category_id VARCHAR(64) NOT NULL,
  brand_hint VARCHAR(64) NULL,
  item_type ENUM('PHYSICAL','SERVICE') NOT NULL DEFAULT 'PHYSICAL'
) ENGINE=InnoDB;
