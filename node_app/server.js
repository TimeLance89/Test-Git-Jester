const path = require('path');
const express = require('express');
const expressLayouts = require('express-ejs-layouts');
const dotenv = require('dotenv');

dotenv.config();

const indexRouter = require('./src/routes/index');
const departmentRouter = require('./src/routes/departments');
const employeeRouter = require('./src/routes/employees');
const scheduleRouter = require('./src/routes/schedule');

const app = express();
const PORT = process.env.APP_PORT || 3000;

app.use(express.urlencoded({ extended: true }));
app.use(express.json());

app.set('views', path.join(__dirname, 'src', 'views'));
app.set('view engine', 'ejs');
app.use(expressLayouts);
app.set('layout', 'layouts/main');

app.use(express.static(path.join(__dirname, 'public')));

app.use((req, res, next) => {
  res.locals.currentPath = req.path;
  next();
});

app.use('/', indexRouter);
app.use('/departments', departmentRouter);
app.use('/employees', employeeRouter);
app.use('/schedule', scheduleRouter);

app.use((req, res) => {
  res.status(404).render('error', {
    title: 'Seite nicht gefunden',
    message: 'Die angeforderte Seite konnte nicht gefunden werden.'
  });
});

app.use((err, req, res, next) => {
  console.error('Unerwarteter Fehler:', err);
  res.status(500).render('error', {
    title: 'Fehler',
    message: 'Es ist ein unerwarteter Fehler aufgetreten. Bitte versuchen Sie es erneut.'
  });
});

app.listen(PORT, () => {
  console.log(`Server läuft auf Port ${PORT}`);
});
