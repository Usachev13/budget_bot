/**
 * Выгрузка данных из бюджетного бота в месячные листы таблицы.
 *
 * Установка: Расширения → Apps Script → вставить этот файл → Сохранить.
 * После этого в таблице появится меню «Бюджет-бот».
 *
 * Скрипт сам добавляет недостающие колонки категорий перед колонкой «Траты»
 * и заполняет суммы по дням. Формулы итогов Google пересчитает сам.
 */

const API_URL = 'https://usachev13.pythonanywhere.com/api/month';
const API_KEY = 'NZTQRIa7rnKcuNYi';          // ключ дашборда

// категория в боте -> колонка в месячном листе
const COLUMNS = {
  'Продукты':            'ТПС',
  'Кафе и доставка':     'Еда вне дома',
  'Красота':             'Красота',
  'Развлечения/подарки': 'Развлечения/Подарки/Покупки',
  'Одежда и покупки':    'Одежда и покупки',
  'Здоровье и спорт':    'Здоровье/спорт',
  'Дети и алименты':     'Дети',
  'Жильё, ЖКХ, связь':   'Дом/подписки',
  'Транспорт':           'Транспорт/доставки',
  'Непредвиденное':      'Непредвиденное',
  'Погашение карты':     'Кредиты/штрафы/налоги',
  'Налоги и взносы ИП':  'Кредиты/штрафы/налоги',
  'Переезд':             'Переезд',
  'Праздники и подарки': 'Развлечения/Подарки/Покупки',
  'Инвестиции':          'Инвестиции',
};

const MONTH_SHEETS = ['Январь', 'Февраль', 'Март', 'Апрель', 'Май', 'Июнь',
                      'Июль', 'Август', 'Сентябрь', 'Октябрь', 'Ноябрь', 'Декабрь'];

function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu('Бюджет-бот')
    .addItem('Обновить текущий месяц', 'обновитьТекущийМесяц')
    .addItem('Обновить выбранный месяц…', 'обновитьВыбранныйМесяц')
    .addToUi();
}

function обновитьТекущийМесяц() {
  const now = new Date();
  const month = Utilities.formatDate(now, 'Europe/Moscow', 'yyyy-MM');
  const result = загрузитьМесяц(month);
  SpreadsheetApp.getActive().toast(result, 'Бюджет-бот', 8);
}

function обновитьВыбранныйМесяц() {
  const ui = SpreadsheetApp.getUi();
  const answer = ui.prompt('Какой месяц обновить?', 'Формат: 2026-09', ui.ButtonSet.OK_CANCEL);
  if (answer.getSelectedButton() !== ui.Button.OK) return;
  const month = answer.getResponseText().trim();
  if (!/^\d{4}-\d{2}$/.test(month)) {
    ui.alert('Нужен формат 2026-09.');
    return;
  }
  ui.alert(загрузитьМесяц(month));
}

/** Основная работа: забирает JSON из бота и раскладывает по дням месяца. */
function загрузитьМесяц(month) {
  const data = запросить(month);
  const [year, mon] = month.split('-').map(Number);
  const sheet = SpreadsheetApp.getActive().getSheetByName(MONTH_SHEETS[mon - 1]);
  if (!sheet) return 'Нет листа ' + MONTH_SHEETS[mon - 1];

  const нужные = новыеКолонки(data);
  const колонки = колонкиЛиста(sheet, нужные);
  const строкиПоДатам = строкиДат(sheet, year, mon);

  // чистим только те ячейки, которые сами же заполняем
  Object.values(строкиПоДатам).forEach(function (row) {
    Object.values(колонки).forEach(function (col) {
      sheet.getRange(row, col).clearContent();
    });
  });

  let записано = 0;
  Object.keys(data.days).forEach(function (date) {
    const row = строкиПоДатам[date];
    if (!row) return;
    const поКолонкам = {};
    Object.keys(data.days[date]).forEach(function (cat) {
      const header = COLUMNS[cat] || 'Непредвиденное';
      поКолонкам[header] = (поКолонкам[header] || 0) + data.days[date][cat];
    });
    Object.keys(поКолонкам).forEach(function (header) {
      const col = колонки[header];
      if (!col) return;
      sheet.getRange(row, col).setValue(Math.round(поКолонкам[header] * 100) / 100);
      записано++;
    });
  });

  записатьДоходы(sheet, data.income);
  return 'Готово: ' + записано + ' значений за ' + month +
         '. Обновлено ' + data.updated.replace('T', ' ');
}

function запросить(month) {
  const url = API_URL + '?key=' + encodeURIComponent(API_KEY) + '&month=' + month;
  const response = UrlFetchApp.fetch(url, { muteHttpExceptions: true });
  if (response.getResponseCode() !== 200) {
    throw new Error('Бот ответил ' + response.getResponseCode());
  }
  return JSON.parse(response.getContentText());
}

/** Какие колонки нужны под данные этого месяца. */
function новыеКолонки(data) {
  const нужные = {};
  Object.keys(data.days).forEach(function (date) {
    Object.keys(data.days[date]).forEach(function (cat) {
      нужные[COLUMNS[cat] || 'Непредвиденное'] = true;
    });
  });
  return Object.keys(нужные);
}

/** Находит колонки по заголовкам, недостающие добавляет перед «Траты». */
function колонкиЛиста(sheet, нужные) {
  let headers = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getValues()[0];
  const найти = function (name) {
    for (let i = 0; i < headers.length; i++) {
      if (String(headers[i]).trim().toLowerCase() === name.toLowerCase()) return i + 1;
    }
    return 0;
  };

  нужные.forEach(function (header) {
    if (найти(header)) return;
    const тратыCol = найти('Траты');
    const insertAt = тратыCol ? тратыCol : sheet.getLastColumn() + 1;
    sheet.insertColumnBefore(insertAt);
    sheet.getRange(1, insertAt).setValue(header);
    headers = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getValues()[0];
  });

  const карта = {};
  нужные.forEach(function (header) { карта[header] = найти(header); });
  return карта;
}

/** Сопоставляет даты из колонки с датами (слева от «ТПС») и номера строк. */
function строкиДат(sheet, year, mon) {
  const last = Math.min(sheet.getLastRow(), 45);
  const values = sheet.getRange(1, 7, last, 1).getValues();   // колонка G
  const карта = {};
  for (let i = 0; i < values.length; i++) {
    const v = values[i][0];
    if (v instanceof Date) {
      const key = Utilities.formatDate(v, 'Europe/Moscow', 'yyyy-MM-dd');
      карта[key] = i + 1;
    }
  }
  // если в шапке листа остался прошлый месяц, даты не совпадут — подставим по номеру дня
  const совпало = Object.keys(карта).filter(function (k) {
    return k.indexOf(year + '-' + ('0' + mon).slice(-2)) === 0;
  }).length;
  if (!совпало) {
    const поДням = {};
    Object.keys(карта).sort().forEach(function (k, idx) {
      const день = ('0' + (idx + 1)).slice(-2);
      поДням[year + '-' + ('0' + mon).slice(-2) + '-' + день] = карта[k];
    });
    return поДням;
  }
  return карта;
}

/** Доходы: ищем подходящую строку в блоке A3:A10, иначе берём первую свободную. */
function записатьДоходы(sheet, income) {
  if (!income) return;
  const имена = sheet.getRange(3, 1, 8, 1).getValues().map(function (r) {
    return String(r[0] || '').toLowerCase();
  });
  const подобрать = function (category) {
    const ключи = {
      'Зарплата': ['зарплат'],
      'Комиссии ИП': ['ринат', 'комисс', 'ип'],
      'Проценты по счетам': ['процент', 'вклад'],
      'Прочие доходы': ['прочие', 'другое'],
    }[category] || [];
    for (let i = 0; i < имена.length; i++) {
      for (let j = 0; j < ключи.length; j++) {
        if (имена[i].indexOf(ключи[j]) >= 0) return i + 3;
      }
    }
    for (let i = 0; i < имена.length; i++) {
      if (!имена[i]) {
        sheet.getRange(i + 3, 1).setValue(category);
        имена[i] = category.toLowerCase();
        return i + 3;
      }
    }
    return 0;
  };

  Object.keys(income).forEach(function (category) {
    const row = подобрать(category);
    if (row) sheet.getRange(row, 2).setValue(Math.round(income[category] * 100) / 100);
  });
}
