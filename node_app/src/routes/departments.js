const express = require('express');
const departmentsRepo = require('../repositories/departments');

const router = express.Router();

router.get('/', async (req, res, next) => {
  try {
    const departments = await departmentsRepo.findAll();
    res.render('departments/index', {
      title: 'Abteilungen',
      departments,
      errors: [],
      values: { name: '' }
    });
  } catch (error) {
    next(error);
  }
});

router.post('/', async (req, res, next) => {
  const { name } = req.body;
  try {
    await departmentsRepo.create(name || '');
    res.redirect('/departments');
  } catch (error) {
    try {
      const departments = await departmentsRepo.findAll();
      res.status(400).render('departments/index', {
        title: 'Abteilungen',
        departments,
        errors: [error.message],
        values: { name: name || '' }
      });
    } catch (innerError) {
      next(innerError);
    }
  }
});

router.post('/:id/delete', async (req, res, next) => {
  const { id } = req.params;
  try {
    await departmentsRepo.remove(id);
    res.redirect('/departments');
  } catch (error) {
    try {
      const departments = await departmentsRepo.findAll();
      res.status(400).render('departments/index', {
        title: 'Abteilungen',
        departments,
        errors: [error.message],
        values: { name: '' }
      });
    } catch (innerError) {
      next(innerError);
    }
  }
});

module.exports = router;
