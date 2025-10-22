const express = require('express');
const { getPool } = require('../db/pool');

const router = express.Router();

router.get('/', async (req, res, next) => {
  try {
    const pool = getPool();
    const [[employeeCount]] = await pool.query(
      'SELECT COUNT(*) AS total FROM employees'
    );
    const [[departmentCount]] = await pool.query(
      'SELECT COUNT(*) AS total FROM departments'
    );
    const [[shiftCount]] = await pool.query(
      `SELECT COUNT(*) AS total
         FROM shifts
        WHERE MONTH(shift_date) = MONTH(CURDATE())
          AND YEAR(shift_date) = YEAR(CURDATE())`
    );

    res.render('home/index', {
      title: 'Dashboard',
      stats: {
        employees: employeeCount.total,
        departments: departmentCount.total,
        shifts: shiftCount.total
      }
    });
  } catch (error) {
    next(error);
  }
});

module.exports = router;
