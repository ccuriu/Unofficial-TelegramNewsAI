from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.colors import HexColor
from reportlab.platypus import Paragraph
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.units import mm

OUT = "Unofficial-TelegramNewsAI-5.5.0-Quick-Start.pdf"
REG = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
pdfmetrics.registerFont(TTFont("DV", REG))
pdfmetrics.registerFont(TTFont("DVB", BOLD))
W, H = A4

COLORS = {
    "navy": HexColor("#22344D"), "text": HexColor("#3F5067"), "muted": HexColor("#6B7F98"),
    "blue": HexColor("#2F6FEB"), "line": HexColor("#9FB2CA"), "box": HexColor("#F6F8FB"),
    "green_bg": HexColor("#EEF9F1"), "green": HexColor("#1F7A42"), "green_line": HexColor("#8ED3A3"),
    "orange_bg": HexColor("#FFF6ED"), "orange": HexColor("#C04C00"), "orange_line": HexColor("#F1B87F"),
    "white": HexColor("#FFFFFF")
}

styles = {}
def style(name, size=10, leading=None, bold=False, color="text"):
    if leading is None:
        leading = size * 1.25
    key = (name, size, leading, bold, color)
    if key not in styles:
        styles[key] = ParagraphStyle(
            name, fontName="DVB" if bold else "DV", fontSize=size,
            leading=leading, textColor=COLORS[color], alignment=TA_LEFT
        )
    return styles[key]

def para(c, text, x, y, w, h, size=10, bold=False, color="text", leading=None):
    p = Paragraph(text, style("p", size, leading, bold, color))
    _, ph = p.wrap(w, h)
    p.drawOn(c, x, y + h - ph)
    return ph

def box(c, x, y, w, h, title, body, fill="box", stroke="line", title_size=11, body_size=9.4):
    c.setFillColor(COLORS[fill])
    c.setStrokeColor(COLORS[stroke])
    c.setLineWidth(0.9)
    c.roundRect(x, y, w, h, 5, fill=1, stroke=1)
    pad = 9
    para(c, title, x + pad, y + h - 34, w - 2 * pad, 26, size=title_size, bold=True, color="navy")
    para(c, body, x + pad, y + 10, w - 2 * pad, h - 48, size=body_size, color="text", leading=body_size * 1.28)

def banner(c, x, y, w, h, text, kind="orange"):
    if kind == "orange":
        bg, fg, ln = "orange_bg", "orange", "orange_line"
    else:
        bg, fg, ln = "green_bg", "green", "green_line"
    c.setFillColor(COLORS[bg])
    c.setStrokeColor(COLORS[ln])
    c.setLineWidth(0.8)
    c.roundRect(x, y, w, h, 4, fill=1, stroke=1)
    para(c, text, x + 9, y + 5, w - 18, h - 10, size=9.2, bold=True, color=fg, leading=11.5)

def arrow(c, x1, y, x2):
    c.setStrokeColor(COLORS["blue"])
    c.setFillColor(COLORS["blue"])
    c.setLineWidth(2.8)
    c.line(x1, y, x2 - 6, y)
    c.line(x2 - 12, y + 6, x2 - 5, y)
    c.line(x2 - 12, y - 6, x2 - 5, y)

def header(c, title, subtitle=None):
    c.setFillColor(COLORS["navy"])
    c.setFont("DVB", 20)
    c.drawString(18 * mm, H - 22 * mm, title)
    if subtitle:
        para(c, subtitle, 18 * mm, H - 39 * mm, W - 36 * mm, 14 * mm, size=9.8, color="text", leading=12.4)

def footer(c, page):
    c.setStrokeColor(HexColor("#DDE4EC"))
    c.setLineWidth(0.6)
    c.line(18 * mm, 14 * mm, W - 18 * mm, 14 * mm)
    c.setFont("DV", 7.4)
    c.setFillColor(COLORS["muted"])
    c.drawString(18 * mm, 9 * mm, "Unofficial TelegramNewsAI 5.5.0 Stable - установка и первый запуск")
    c.drawRightString(W - 18 * mm, 9 * mm, f"стр. {page}")

c = canvas.Canvas(OUT, pagesize=A4)
c.setTitle("Unofficial TelegramNewsAI 5.5.0 Stable - установка и первый запуск")

# PAGE 1
para(c, "<b>Unofficial TelegramNewsAI 5.5.0 Stable -<br/>установка и первый запуск</b>", 18*mm, H-48*mm, W-36*mm, 32*mm, size=20, bold=True, color="navy", leading=22)
para(c, "Инструкция для обычного пользователя: от скачивания Stable Release до первого готового дайджеста.", 18*mm, H-62*mm, W-36*mm, 11*mm, size=9.8, color="text", leading=12.4)
banner(c, 18*mm, H-82*mm, W-36*mm, 16*mm, "Главное: распакуйте ZIP в постоянную папку. Установщик работает прямо в ней и создаёт ярлык. После установки папку не удаляйте и без необходимости не переносите.", "orange")

c.setFont("DVB", 16); c.setFillColor(COLORS["navy"]); c.drawString(18*mm, H-94*mm, "1. Скачайте, распакуйте и установите")
y = H-139*mm; bw = 55*mm; bh = 38*mm; gap = 7*mm; x0 = 18*mm
box(c, x0, y, bw, bh, "1. Скачать Stable Release", "Откройте страницу <b>Releases</b> и скачайте файл <b>Unofficial-TelegramNewsAI-5.5.0-Stable-Windows.zip</b>.")
box(c, x0+bw+gap, y, bw, bh, "2. Распаковать ZIP", "Создайте постоянную папку, например <b>Документы\\TelegramNewsAI</b>. Правой кнопкой по архиву -> <b>Извлечь все...</b>.")
box(c, x0+2*(bw+gap), y, bw, bh, "3. Запустить INSTALL.bat", "Откройте уже распакованную папку, дважды щёлкните <b>INSTALL.bat</b> и дождитесь сообщения об успешной установке.")
arrow(c, x0+bw+2*mm, y+bh/2, x0+bw+gap-2*mm); arrow(c, x0+2*bw+gap+2*mm, y+bh/2, x0+2*(bw+gap)-2*mm)
banner(c, 18*mm, H-158*mm, W-36*mm, 16*mm, "Если подходящего Python нет, установщик может установить Python 3.13 через Windows Package Manager. После установки на рабочем столе появится ярлык Unofficial TelegramNewsAI.", "green")

c.setFont("DVB", 16); c.setFillColor(COLORS["navy"]); c.drawString(18*mm, H-171*mm, "2. Получите API ID и API Hash")
y2 = H-215*mm
box(c, x0, y2, bw, bh, "1. Открыть my.telegram.org", "Войдите по номеру своего Telegram-аккаунта. Код подтверждения придёт в Telegram.")
box(c, x0+bw+gap, y2, bw, bh, "2. API development tools", "Если Telegram-приложение для этого номера уже создавалось, используйте существующие данные. Новое создавать не обязательно.")
box(c, x0+2*(bw+gap), y2, bw, bh, "3. Подготовить два значения", "<b>api_id</b> - число.<br/><b>api_hash</b> - строка из 32 символов 0-9 и a-f. Они понадобятся при первом запуске.")
arrow(c, x0+bw+2*mm, y2+bh/2, x0+bw+gap-2*mm); arrow(c, x0+2*bw+gap+2*mm, y2+bh/2, x0+2*(bw+gap)-2*mm)
banner(c, 18*mm, H-232*mm, W-36*mm, 14*mm, "Не передавайте другим людям API ID, API Hash, код входа, пароль 2FA, credentials.bin и файлы telegram_session.session.", "orange")
footer(c, 1); c.showPage()

# PAGE 2
header(c, "Первый вход в Telegram и выбор каналов", "Запустите программу ярлыком Unofficial TelegramNewsAI на рабочем столе. Дальше вводите данные по очереди.")
c.setFont("DVB", 16); c.setFillColor(COLORS["navy"]); c.drawString(18*mm, H-51*mm, "3. Первый запуск: что и куда вводить")
left = 18*mm; right = 107*mm; ww = 84*mm; hh = 38*mm
y = H-95*mm
box(c, left, y, ww, hh, "Шаг 1 - API ID", "Когда увидите <b>Введите API ID:</b>, введите только число с my.telegram.org и нажмите Enter.")
box(c, right, y, ww, hh, "Шаг 2 - API Hash", "Щёлкните правой кнопкой мыши в окне -> <b>Вставить</b>, затем нажмите <b>Enter</b>. Нужно вставить все 32 символа. Ввод скрытый - символы могут не отображаться.")
y -= 46*mm
box(c, left, y, ww, hh, "Шаг 3 - номер телефона", "Введите номер, к которому привязан ваш Telegram-аккаунт, в международном формате: <b>+ код страны и номер</b>. Затем нажмите <b>Enter</b>.")
box(c, right, y, ww, hh, "Шаг 4 - код и 2FA", "Введите код подтверждения из Telegram. Если включена двухэтапная защита, затем введите пароль Telegram.")
banner(c, 18*mm, H-166*mm, W-36*mm, 20*mm, "Для API Hash используйте правую кнопку мыши -> Вставить. Если вставка не сработала, запасной вариант - Shift+Insert. Если программа пишет, что длина Hash неправильная, убедитесь, что вставлены все 32 символа без пробелов.", "orange")

c.setFont("DVB", 16); c.setFillColor(COLORS["navy"]); c.drawString(18*mm, H-179*mm, "4. Выберите каналы для первого теста")
box(c, 18*mm, H-244*mm, 92*mm, 55*mm, "Что можно вводить", "- номера каналов из списка;<br/>- диапазоны, например <b>1,3,7-10</b>;<br/>- <b>@username</b>;<br/>- ссылку <b>https://t.me/channel</b>.<br/><br/>В одной строке можно смешивать номера и Telegram-адреса. Публичный канал можно добавить даже без подписки.", body_size=9.2)
box(c, 115*mm, H-244*mm, 77*mm, 55*mm, "Что делать", "Для первого запуска выберите <b>5-10 реальных каналов</b>.<br/><br/>После основного выбора программа предложит необязательно добавить ещё публичные каналы. Если добавлять нечего - просто нажмите Enter.<br/><br/><b>Максимум: 50 источников.</b>", body_size=9.2)
banner(c, 18*mm, 36*mm, W-36*mm, 14*mm, "Совет: сначала проверьте работу на 5-10 каналах. Большой список можно настроить позже.", "green")
footer(c, 2); c.showPage()

# PAGE 3
header(c, "Первый дайджест и готовый результат", "После сохранения списка каналов программа вернётся к обычному сценарию создания дайджеста.")
c.setFont("DVB", 16); c.setFillColor(COLORS["navy"]); c.drawString(18*mm, H-51*mm, "5. Создайте первый дайджест")
y = H-104*mm; bw = 55*mm; bh = 46*mm; gap = 7*mm; x0 = 18*mm
box(c, x0, y, bw, bh, "1. Выберите обычный дайджест", "В главном меню нажмите Enter или введите <b>D</b>. Программа использует сохранённый список каналов.", body_size=9.1)
box(c, x0+bw+gap, y, bw, bh, "2. Выберите период", "На вопрос <b>За сколько последних часов сделать выгрузку? [24]</b> для первого теста просто нажмите Enter.", body_size=9.1)
box(c, x0+2*(bw+gap), y, bw, bh, "3. Дождитесь завершения", "Программа синхронизирует сообщения, соберёт период, обработает связанные публикации и сохранит файлы. Первый запуск может быть дольше следующих.", body_size=8.8)
arrow(c, x0+bw+2*mm, y+bh/2, x0+bw+gap-2*mm); arrow(c, x0+2*bw+gap+2*mm, y+bh/2, x0+2*(bw+gap)-2*mm)
box(c, 18*mm, H-151*mm, 84*mm, 34*mm, "Пример", "Выбор [Enter/D/...]: <b>D</b><br/>За сколько последних часов сделать выгрузку? [24]:<br/>Для 24 часов просто нажмите Enter.", body_size=8.8)
box(c, 107*mm, H-151*mm, 85*mm, 34*mm, "Во время работы", "Не закрывайте окно, пока программа не закончит сбор. Если период загружен не полностью, сохраните результат и текст сообщения - это полезно для диагностики.", body_size=8.7)

c.setFont("DVB", 16); c.setFillColor(COLORS["navy"]); c.drawString(18*mm, H-164*mm, "6. Где находится результат и что с ним делать")
box(c, 18*mm, H-230*mm, 88*mm, 57*mm, "ДАЙДЖЕСТ_ДЛЯ_ИИ.md - основной файл", "После успешного завершения Проводник Windows автоматически откроется и выделит файл.<br/><br/>Это готовый Markdown-дайджест. Его можно передать выбранному внешнему интеллектуальному агенту или ассистенту для общего обзора событий либо разбора конкретной темы.", body_size=8.7)
box(c, 111*mm, H-230*mm, 81*mm, 57*mm, "ДАЙДЖЕСТ_ПОСЛЕДНИЙ.json", "Полный структурированный экспорт schema 8. Он нужен для проверки данных, совместимости и технического анализа.<br/><br/>Для обычной работы чаще достаточно Markdown-файла.", body_size=8.9)
banner(c, 18*mm, H-244*mm, W-36*mm, 12*mm, "TelegramNewsAI сам не отправляет Telegram-содержимое во внешние AI/ML-сервисы. Пользователь сам выбирает, какой готовый файл и куда передать.", "green")
box(c, 18*mm, 22*mm, 89*mm, 27*mm, "Что прислать для диагностики", "Что делали; что ожидали; что получилось; полный текст ошибки или скриншот; при необходимости - лог из папки logs.", body_size=8.2)
box(c, 112*mm, 22*mm, 80*mm, 27*mm, "Чего не присылать", "API Hash, код входа, пароль 2FA, credentials.bin и файлы telegram_session.session.", body_size=8.2)
footer(c, 3); c.save()
print(OUT)
