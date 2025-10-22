CREATE TABLE IF NOT EXISTS departments (
  id INT AUTO_INCREMENT PRIMARY KEY,
  name VARCHAR(120) NOT NULL UNIQUE,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS employees (
  id INT AUTO_INCREMENT PRIMARY KEY,
  department_id INT NULL,
  name VARCHAR(120) NOT NULL,
  email VARCHAR(255) NULL,
  employment_type ENUM('full_time', 'part_time') NOT NULL DEFAULT 'full_time',
  hours_per_month DECIMAL(5,2) NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT fk_employees_department
    FOREIGN KEY (department_id) REFERENCES departments(id)
    ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS shifts (
  id INT AUTO_INCREMENT PRIMARY KEY,
  employee_id INT NOT NULL,
  shift_date DATE NOT NULL,
  start_time TIME NOT NULL,
  end_time TIME NOT NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT fk_shifts_employee
    FOREIGN KEY (employee_id) REFERENCES employees(id)
    ON DELETE CASCADE,
  CONSTRAINT ck_shift_time CHECK (start_time < end_time)
);

CREATE INDEX idx_shifts_date ON shifts (shift_date);
