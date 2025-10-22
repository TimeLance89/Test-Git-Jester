const { getPool } = require('../db/pool');

async function findByMonth(year, month) {
  const pool = getPool();
  const [rows] = await pool.query(
    `SELECT s.id,
            s.employee_id,
            s.shift_date,
            s.start_time,
            s.end_time,
            e.name AS employee_name
       FROM shifts s
       JOIN employees e ON e.id = s.employee_id
      WHERE YEAR(s.shift_date) = ?
        AND MONTH(s.shift_date) = ?
      ORDER BY s.shift_date ASC, s.start_time ASC`,
    [year, month]
  );
  return rows;
}

async function create({ employeeId, shiftDate, startTime, endTime }) {
  const pool = getPool();
  await pool.query(
    `INSERT INTO shifts (employee_id, shift_date, start_time, end_time)
     VALUES (?, ?, ?, ?)`,
    [employeeId, shiftDate, startTime, endTime]
  );
}

async function remove(id) {
  const pool = getPool();
  await pool.query('DELETE FROM shifts WHERE id = ?', [id]);
}

module.exports = {
  findByMonth,
  create,
  remove
};
