const { getPool } = require('../db/pool');

async function findAll() {
  const pool = getPool();
  const [rows] = await pool.query(
    'SELECT id, name, created_at FROM departments ORDER BY name ASC'
  );
  return rows;
}

async function create(name) {
  const pool = getPool();
  const trimmed = name.trim();
  if (!trimmed) {
    throw new Error('Der Abteilungsname darf nicht leer sein.');
  }
  await pool.query('INSERT INTO departments (name) VALUES (?)', [trimmed]);
}

async function remove(id) {
  const pool = getPool();
  const conn = await pool.getConnection();
  try {
    await conn.beginTransaction();
    const [usage] = await conn.query(
      'SELECT COUNT(*) AS count FROM employees WHERE department_id = ?',
      [id]
    );
    if (usage[0].count > 0) {
      throw new Error('Die Abteilung kann nicht gelöscht werden, da ihr Mitarbeiter zugeordnet sind.');
    }
    await conn.query('DELETE FROM departments WHERE id = ?', [id]);
    await conn.commit();
  } catch (error) {
    await conn.rollback();
    throw error;
  } finally {
    conn.release();
  }
}

module.exports = {
  findAll,
  create,
  remove
};
