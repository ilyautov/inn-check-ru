<!-- Часть скилла inn-check-ru. Отдельный файл, потому что SKILL.md читается
целиком при каждом запуске, а это — справка по запросу. -->

# Своя нода в РФ

**Читать, когда `check_access.py` показывает «гео» и пользователь спрашивает,
как это обойти.** Ответ — свой узел в России, не чужой.

## Зачем и чего не будет

С не-РФ IP скрипт не достаёт zakupki, НПД и детальный эндпоинт «Прозрачного
бизнеса» — нода их открывает. Суды, ФССП и банкротство ИП нода не решает: они
скриптом не берутся вообще, это браузерный шаг (`references/brauzer.md`). У проекта **нет и не будет** общего прокси-пула и
платных прокси-API: узел в середине видит, **какие ИНН вы проверяете**, а список
проверяемых контрагентов — коммерческая тайна и карта ваших сделок до их
заключения. Содержимое узел подменить не может, пока проверяется TLS
(https идёт CONNECT-туннелем); с `COUNTERPARTY_INSECURE=1` — может, и такое
сочетание кричит в stderr.

## Рецепт: VPS + squid с паролем

Нужен VPS с российским IP у любого провайдера (минимальный тариф хватает).

```sh
# на VPS (Debian/Ubuntu)
sudo apt install -y squid apache2-utils
sudo htpasswd -c /etc/squid/passwd <логин>          # пароль спросит интерактивно
sudo tee /etc/squid/squid.conf >/dev/null <<'EOF'
http_port 3128
auth_param basic program /usr/lib/squid/basic_ncsa_auth /etc/squid/passwd
auth_param basic realm inn-check
acl auth proxy_auth REQUIRED
acl SSL_ports port 443
acl CONNECT method CONNECT
http_access deny CONNECT !SSL_ports
http_access allow auth
http_access deny all
access_log none
cache deny all
EOF
sudo systemctl restart squid
sudo ufw allow from <ваш_IP> to any port 3128 proto tcp   # порт — только себе
```

- **Пароль обязателен** и порт закрыт файрволом для всех, кроме вас: открытый
  прокси найдут сканеры за часы.
- `access_log none` — узел не пишет, кого вы проверяли.
- socks5 не подойдёт: stdlib urllib умеет только http/https-прокси, и скрипт
  откажет, а не пойдёт напрямую.

## Подключение

```sh
export INN_CHECK_PROXY='http://<логин>:<пароль>@<хост>:3128'
python3 scripts/check_access.py            # или --прокси URL
python3 scripts/fetch_counterparty.py <ИНН>
```

Порядок: `--прокси` > `INN_CHECK_PROXY` > `HTTPS_PROXY`. Учётку держите в
переменной окружения или менеджере секретов — не в репозитории, не в отчётах и
не в чате с агентом. Пароль в выводе маскируется (`http://user:***@host`).

## Что проверить

1. `check_access.py` через узел: «гео» у zakupki и НПД ушло.
   Кэш probe помечен отпечатком сети — снятый напрямую не переиспользуется.
2. Блок `_сеть` в выводе `fetch_counterparty.py` называет узел (с маской
   пароля) — значит, шли через него.
3. Без `--прокси` и переменных соединение строго прямое: в `_сеть` отпечаток «прямое».
4. Строки kad.arbitr и ФССП в таблице `check_access` — только доступность
   сайта: скриптом они не собираются и через ноду.
