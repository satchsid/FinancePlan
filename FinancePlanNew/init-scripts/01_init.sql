-- Schema
CREATE TABLE IF NOT EXISTS transactions (
    id SERIAL PRIMARY KEY,
    date DATE NOT NULL,
    description TEXT NOT NULL,
    amount NUMERIC(12,2) NOT NULL DEFAULT 0,
    category TEXT NOT NULL DEFAULT '',
    account TEXT NOT NULL DEFAULT '',
    type TEXT NOT NULL DEFAULT 'Expense' CHECK (type IN ('Income', 'Expense'))
);

CREATE TABLE IF NOT EXISTS starting_balances (
    id SERIAL PRIMARY KEY,
    month DATE NOT NULL,
    account TEXT NOT NULL,
    amount NUMERIC(12,2) NOT NULL DEFAULT 0,
    UNIQUE (month, account)
);

CREATE TABLE IF NOT EXISTS fixed_templates (
    id SERIAL PRIMARY KEY,
    description TEXT NOT NULL,
    amount NUMERIC(12,2) NOT NULL DEFAULT 0,
    due_day INT NOT NULL DEFAULT 1,
    category TEXT NOT NULL DEFAULT '',
    account TEXT NOT NULL DEFAULT '',
    type TEXT NOT NULL DEFAULT 'Expense'
);

-- Seed: finance_data.csv
INSERT INTO transactions (date, description, amount, category, account, type) VALUES
('2026-03-26', 'Internet', 65.18, 'Utilities', 'Chase', 'Expense'),
('2026-03-26', 'Robinhood', 1631.71, 'Credit Card', 'Wealthfront', 'Expense'),
('2026-03-26', 'Citi-8133', 32.99, 'Credit Card', 'Wealthfront', 'Expense'),
('2026-03-23', 'Affirm - Washer', 40.58, 'Debt', 'Wealthfront', 'Expense'),
('2026-03-15', 'Amazon', 166.89, 'Credit Card Bill', 'Chase', 'Expense'),
('2026-03-15', 'Discover', 550.58, 'Credit Card Bill', 'Chase', 'Expense'),
('2026-03-06', 'Capital One', 225.02, 'Credit Card Bill', 'Chase', 'Expense'),
('2026-03-06', 'Chase', 129.81, 'Credit Card Bill', 'Chase', 'Expense'),
('2026-03-01', 'Mortgage Loan', 1774.72, 'Housing', 'Wealthfront', 'Expense'),
('2026-03-01', 'HOA', 165.00, 'Housing', 'Wealthfront', 'Expense'),
('2026-03-02', 'AB', 500.00, 'Bills', 'Chase', 'Expense'),
('2026-03-03', 'Apple Card', 114.82, 'Credit Card', 'Wealthfront', 'Expense'),
('2026-03-15', 'Salary', 4500.00, 'Income', 'Wealthfront', 'Income'),
('2026-02-01', 'Mortgage Loan', 1774.72, 'Housing', 'Wealthfront', 'Expense'),
('2026-02-01', 'HOA', 165.00, 'Housing', 'Wealthfront', 'Expense'),
('2026-02-15', 'Salary', 4500.00, 'Income', 'Wealthfront', 'Income'),
('2026-02-10', 'Groceries', 320.50, 'Food', 'Chase', 'Expense'),
('2026-02-14', 'Restaurant', 85.00, 'Dining', 'Chase', 'Expense'),
('2026-01-01', 'Mortgage Loan', 1774.72, 'Housing', 'Wealthfront', 'Expense'),
('2026-01-01', 'HOA', 165.00, 'Housing', 'Wealthfront', 'Expense'),
('2026-01-15', 'Salary', 4500.00, 'Income', 'Wealthfront', 'Income'),
('2026-01-20', 'Car Insurance', 145.00, 'Insurance', 'Chase', 'Expense')
ON CONFLICT DO NOTHING;

-- Seed: starting_balances.csv
INSERT INTO starting_balances (month, account, amount) VALUES
('2026-01-01', 'Wealthfront', 8022.14),
('2026-01-01', 'Chase', 1479.14),
('2026-01-01', 'DCU', 600.00),
('2026-02-01', 'Wealthfront', 7500.00),
('2026-02-01', 'Chase', 1200.00),
('2026-02-01', 'DCU', 600.00),
('2026-03-01', 'Wealthfront', 7884.10),
('2026-03-01', 'Chase', 1100.00),
('2026-03-01', 'DCU', 600.00)
ON CONFLICT (month, account) DO NOTHING;

-- Seed: fixed_templates.csv
INSERT INTO fixed_templates (description, amount, due_day, category, account, type) VALUES
('Mortgage Loan', 1774.72, 1, 'Housing', 'Wealthfront', 'Expense'),
('HOA', 165.00, 1, 'Housing', 'Wealthfront', 'Expense'),
('AB', 500.00, 2, 'Bills', 'Chase', 'Expense'),
('Apple Card', 114.82, 3, 'Credit Card', 'Wealthfront', 'Expense'),
('Internet', 65.18, 26, 'Utilities', 'Chase', 'Expense'),
('Car Insurance', 145.00, 20, 'Insurance', 'Chase', 'Expense')
ON CONFLICT DO NOTHING;

-- bank_statements intentionally has no table here.
-- The external automation service writes directly to data/bank_statements.csv
-- on the host, which is mounted read-only into the backend container.
-- The /statements and /statements/sync API endpoints read from that CSV file.
