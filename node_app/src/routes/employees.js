const express = require('express');
const employeesRepo = require('../repositories/employees');
const departmentsRepo = require('../repositories/departments');

const router = express.Router();

function parseEmployeeForm(body) {
  const name = body.name ? body.name.trim() : '';
  const email = body.email ? body.email.trim() : '';
  const employmentType = body.employment_type || 'full_time';
  const hoursRaw = body.hours_per_month ? body.hours_per_month.trim() : '';
  const departmentRaw = body.department_id ? body.department_id.trim() : '';

  const errors = [];

  if (!name) {
    errors.push('Der Name darf nicht leer sein.');
  }

  if (email && !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) {
    errors.push('Bitte geben Sie eine gültige E-Mail-Adresse an.');
  }

  if (!['full_time', 'part_time'].includes(employmentType)) {
    errors.push('Bitte wählen Sie eine gültige Beschäftigungsart aus.');
  }

  let hoursPerMonth = null;
  if (hoursRaw) {
    const parsed = Number(hoursRaw);
    if (Number.isNaN(parsed) || parsed < 0) {
      errors.push('Die monatlichen Stunden müssen eine positive Zahl sein.');
    } else {
      hoursPerMonth = parsed;
    }
  }

  let departmentId = null;
  if (departmentRaw) {
    const parsed = Number(departmentRaw);
    if (Number.isNaN(parsed) || parsed < 1) {
      errors.push('Die gewählte Abteilung ist ungültig.');
    } else {
      departmentId = parsed;
    }
  }

  return {
    errors,
    employee: {
      name,
      email: email || null,
      employmentType,
      hoursPerMonth,
      departmentId
    },
    values: {
      name,
      email,
      employmentType,
      hoursPerMonth: hoursRaw,
      departmentId: departmentRaw
    }
  };
}

router.get('/', async (req, res, next) => {
  try {
    const employees = await employeesRepo.findAll();
    res.render('employees/index', {
      title: 'Mitarbeiter',
      employees
    });
  } catch (error) {
    next(error);
  }
});

router.get('/new', async (req, res, next) => {
  try {
    const departments = await departmentsRepo.findAll();
    res.render('employees/form', {
      title: 'Mitarbeiter anlegen',
      departments,
      errors: [],
      values: {
        name: '',
        email: '',
        employmentType: 'full_time',
        hoursPerMonth: '',
        departmentId: ''
      },
      formAction: '/employees',
      submitLabel: 'Mitarbeiter speichern',
      isEdit: false
    });
  } catch (error) {
    next(error);
  }
});

router.post('/', async (req, res, next) => {
  const { errors, employee, values } = parseEmployeeForm(req.body);
  if (errors.length > 0) {
    try {
      const departments = await departmentsRepo.findAll();
      res.status(400).render('employees/form', {
        title: 'Mitarbeiter anlegen',
        departments,
        errors,
        values,
        formAction: '/employees',
        submitLabel: 'Mitarbeiter speichern',
        isEdit: false
      });
    } catch (error) {
      next(error);
    }
    return;
  }

  try {
    await employeesRepo.create(employee);
    res.redirect('/employees');
  } catch (error) {
    next(error);
  }
});

router.get('/:id/edit', async (req, res, next) => {
  const { id } = req.params;
  try {
    const employee = await employeesRepo.findById(id);
    if (!employee) {
      res.status(404).render('error', {
        title: 'Nicht gefunden',
        message: 'Der gewünschte Mitarbeiter konnte nicht gefunden werden.'
      });
      return;
    }
    const departments = await departmentsRepo.findAll();
    res.render('employees/form', {
      title: 'Mitarbeiter bearbeiten',
      departments,
      errors: [],
      values: {
        name: employee.name,
        email: employee.email || '',
        employmentType: employee.employmentType,
        hoursPerMonth: employee.hoursPerMonth ? String(employee.hoursPerMonth) : '',
        departmentId: employee.departmentId ? String(employee.departmentId) : ''
      },
      formAction: `/employees/${id}`,
      submitLabel: 'Änderungen speichern',
      isEdit: true
    });
  } catch (error) {
    next(error);
  }
});

router.post('/:id', async (req, res, next) => {
  const { id } = req.params;
  const { errors, employee, values } = parseEmployeeForm(req.body);
  if (errors.length > 0) {
    try {
      const departments = await departmentsRepo.findAll();
      res.status(400).render('employees/form', {
        title: 'Mitarbeiter bearbeiten',
        departments,
        errors,
        values,
        formAction: `/employees/${id}`,
        submitLabel: 'Änderungen speichern',
        isEdit: true
      });
    } catch (error) {
      next(error);
    }
    return;
  }

  try {
    await employeesRepo.update(id, employee);
    res.redirect('/employees');
  } catch (error) {
    next(error);
  }
});

router.post('/:id/delete', async (req, res, next) => {
  const { id } = req.params;
  try {
    await employeesRepo.remove(id);
    res.redirect('/employees');
  } catch (error) {
    next(error);
  }
});

module.exports = router;
