const express = require('express');
const shiftsRepo = require('../repositories/shifts');
const employeesRepo = require('../repositories/employees');

const router = express.Router();

function parseMonthYear(query) {
  const now = new Date();
  let month = Number.parseInt(query.month, 10);
  let year = Number.parseInt(query.year, 10);

  if (!Number.isInteger(month) || month < 1 || month > 12) {
    month = now.getMonth() + 1;
  }
  if (!Number.isInteger(year) || year < 1970) {
    year = now.getFullYear();
  }

  return { month, year };
}

function getMonthLabel(month) {
  const formatter = new Intl.DateTimeFormat('de-DE', { month: 'long' });
  const date = new Date(2020, month - 1, 1);
  return formatter.format(date);
}

function getNavigation(year, month) {
  const prevDate = new Date(year, month - 2, 1);
  const nextDate = new Date(year, month, 1);
  return {
    previous: { month: prevDate.getMonth() + 1, year: prevDate.getFullYear() },
    next: { month: nextDate.getMonth() + 1, year: nextDate.getFullYear() }
  };
}

function groupShiftsByDate(shifts) {
  return shifts.reduce((acc, shift) => {
    const dateKey = shift.shift_date instanceof Date
      ? shift.shift_date.toISOString().slice(0, 10)
      : shift.shift_date;
    if (!acc[dateKey]) {
      acc[dateKey] = [];
    }
    acc[dateKey].push({
      id: shift.id,
      employeeId: shift.employee_id,
      employeeName: shift.employee_name,
      startTime: shift.start_time,
      endTime: shift.end_time
    });
    return acc;
  }, {});
}

function getDefaultFormValues({ month, year }) {
  const today = new Date();
  const defaultDate = today.getFullYear() === year && today.getMonth() + 1 === month
    ? today.toISOString().slice(0, 10)
    : new Date(year, month - 1, 1).toISOString().slice(0, 10);
  return {
    employeeId: '',
    shiftDate: defaultDate,
    startTime: '09:00',
    endTime: '17:00'
  };
}

async function renderSchedule(res, options) {
  const { month, year } = options;
  const [employees, shifts] = await Promise.all([
    employeesRepo.findBasicList(),
    shiftsRepo.findByMonth(year, month)
  ]);

  res.render('schedule/index', {
    title: 'Dienstplan',
    month,
    year,
    monthLabel: getMonthLabel(month),
    navigation: getNavigation(year, month),
    shiftsByDate: groupShiftsByDate(shifts),
    employees,
    errors: options.errors || [],
    formValues: options.formValues || getDefaultFormValues({ month, year })
  });
}

router.get('/', async (req, res, next) => {
  const { month, year } = parseMonthYear(req.query);
  try {
    await renderSchedule(res, { month, year });
  } catch (error) {
    next(error);
  }
});

router.post('/shifts', async (req, res, next) => {
  const { month, year } = parseMonthYear(req.body);
  const formValues = {
    employeeId: req.body.employee_id || '',
    shiftDate: req.body.shift_date || '',
    startTime: req.body.start_time || '',
    endTime: req.body.end_time || ''
  };

  const errors = [];

  const employeeId = Number.parseInt(formValues.employeeId, 10);
  if (!Number.isInteger(employeeId)) {
    errors.push('Bitte wählen Sie einen Mitarbeiter aus.');
  }

  if (!formValues.shiftDate || Number.isNaN(Date.parse(formValues.shiftDate))) {
    errors.push('Bitte geben Sie ein gültiges Datum an.');
  }

  const validTime = /^\d{2}:\d{2}$/;
  if (!validTime.test(formValues.startTime) || !validTime.test(formValues.endTime)) {
    errors.push('Bitte geben Sie gültige Start- und Endzeiten an.');
  } else {
    const startMinutes = parseInt(formValues.startTime.replace(':', ''), 10);
    const endMinutes = parseInt(formValues.endTime.replace(':', ''), 10);
    if (startMinutes >= endMinutes) {
      errors.push('Die Endzeit muss nach der Startzeit liegen.');
    }
  }

  if (errors.length > 0) {
    try {
      await renderSchedule(res, { month, year, errors, formValues });
    } catch (error) {
      next(error);
    }
    return;
  }

  try {
    await shiftsRepo.create({
      employeeId,
      shiftDate: formValues.shiftDate,
      startTime: formValues.startTime,
      endTime: formValues.endTime
    });
    res.redirect(`/schedule?month=${month}&year=${year}`);
  } catch (error) {
    errors.push('Die Schicht konnte nicht gespeichert werden. Bitte versuchen Sie es erneut.');
    try {
      await renderSchedule(res, { month, year, errors, formValues });
    } catch (innerError) {
      next(innerError);
    }
  }
});

router.post('/shifts/:id/delete', async (req, res, next) => {
  const { id } = req.params;
  const { month, year } = parseMonthYear(req.body);
  try {
    await shiftsRepo.remove(id);
    res.redirect(`/schedule?month=${month}&year=${year}`);
  } catch (error) {
    try {
      await renderSchedule(res, {
        month,
        year,
        errors: ['Die Schicht konnte nicht gelöscht werden. Bitte versuchen Sie es erneut.'],
        formValues: getDefaultFormValues({ month, year })
      });
    } catch (innerError) {
      next(innerError);
    }
  }
});

module.exports = router;
