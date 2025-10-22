const { getPool } = require('../db/pool');

function mapEmployee(row) {
  if (!row) {
    return null;
  }
  return {
    id: row.id,
    name: row.name,
    email: row.email,
    employmentType: row.employment_type,
    hoursPerMonth: row.hours_per_month,
    departmentId: row.department_id,
    departmentName: row.department_name || null,
    createdAt: row.created_at
  };
}

async function findAll() {
  const pool = getPool();
  const [rows] = await pool.query(
    `SELECT e.id,
            e.name,
            e.email,
            e.employment_type,
            e.hours_per_month,
            e.department_id,
            e.created_at,
            d.name AS department_name
       FROM employees e
       LEFT JOIN departments d ON d.id = e.department_id
      ORDER BY e.name ASC`
  );
  return rows.map(mapEmployee);
}

async function findBasicList() {
  const pool = getPool();
  const [rows] = await pool.query(
    'SELECT id, name FROM employees ORDER BY name ASC'
  );
  return rows;
}

async function findById(id) {
  const pool = getPool();
  const [rows] = await pool.query(
    `SELECT e.id,
            e.name,
            e.email,
            e.employment_type,
            e.hours_per_month,
            e.department_id,
            e.created_at,
            d.name AS department_name
       FROM employees e
       LEFT JOIN departments d ON d.id = e.department_id
      WHERE e.id = ?`,
    [id]
  );
  return mapEmployee(rows[0]);
}

async function create(employee) {
  const pool = getPool();
  const {
    name,
    email,
    employmentType,
    hoursPerMonth,
    departmentId
  } = employee;

  await pool.query(
    `INSERT INTO employees (name, email, employment_type, hours_per_month, department_id)
     VALUES (?, ?, ?, ?, ?)`,
    [
      name.trim(),
      email ? email.trim() : null,
      employmentType,
      hoursPerMonth || null,
      departmentId || null
    ]
  );
}

async function update(id, employee) {
  const pool = getPool();
  const {
    name,
    email,
    employmentType,
    hoursPerMonth,
    departmentId
  } = employee;

  await pool.query(
    `UPDATE employees
        SET name = ?,
            email = ?,
            employment_type = ?,
            hours_per_month = ?,
            department_id = ?
      WHERE id = ?`,
    [
      name.trim(),
      email ? email.trim() : null,
      employmentType,
      hoursPerMonth || null,
      departmentId || null,
      id
    ]
  );
}

async function remove(id) {
  const pool = getPool();
  await pool.query('DELETE FROM employees WHERE id = ?', [id]);
}

module.exports = {
  findAll,
  findBasicList,
  findById,
  create,
  update,
  remove
};
