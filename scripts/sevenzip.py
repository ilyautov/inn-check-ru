#!/usr/bin/env python3
"""
sevenzip.py — чтение 7z-архива с одним файлом, потоком. Только stdlib (lzma).

Росаккредитация публикует открытые данные в 7z; zipfile его не читает, а тащить
зависимость ради одного формата проект не будет. Модуль разбирает контейнер 7z
сам и распаковывает поток через lzma.LZMADecompressor в «сыром» режиме.

Поддержано ровно то, что встречается в выгрузках (живьём 28.09.2026: один CSV,
один блок, кодек LZMA2, заголовок не сжат):
    * один файл в архиве, один блок (folder), один кодек — LZMA2 или LZMA;
    * заголовок обычный или сжатый (kEncodedHeader) тем же кодеком;
    * CRC32 распакованного файла сверяется в конце.
Всё остальное — фильтры (BCJ, Delta), шифрование, несколько файлов или блоков —
ValueError «7z: … не поддерживается», а не попытка угадать.

    for кусок in sevenzip.читать(путь): ...
    with sevenzip.открыть(путь) as fh: ...      # файлоподобный объект, байты
    имя, размер = sevenzip.оглавление(путь)
"""

import io
import lzma
import struct
import zlib

СИГНАТУРА = b"7z\xbc\xaf\x27\x1c"
LZMA2, LZMA1 = b"\x21", b"\x03\x01\x01"
МАКС_СЛОВАРЬ = 1 << 30      # живьём 64 МБ; больше — враждебный архив, а не выгрузка
МАКС_ЗАГОЛОВОК = 64 << 20


class _Буфер:
    def __init__(self, данные):
        self.b, self.i = данные, 0

    def байт(self):
        if self.i >= len(self.b):
            raise ValueError("7z: заголовок оборван")
        self.i += 1
        return self.b[self.i - 1]

    def байты(self, n):
        if self.i + n > len(self.b):
            raise ValueError("7z: заголовок оборван")
        self.i += n
        return self.b[self.i - n:self.i]

    def число(self):
        """UINT64 7z: число старших единиц первого байта — сколько байт дальше."""
        первый, маска, знач = self.байт(), 0x80, 0
        for n in range(8):
            if not первый & маска:
                return знач | ((первый & (маска - 1)) << (8 * n))
            знач |= self.байт() << (8 * n)
            маска >>= 1
        return знач

    def ждать(self, тип):
        t = self.байт()
        if t != тип:
            raise ValueError("7z: ожидался тег %#x, а не %#x" % (тип, t))


def _биты(буф, n):
    все = буф.байт()
    if все:
        return [True] * n
    out, байт, маска = [], 0, 0
    for _ in range(n):
        if not маска:
            байт, маска = буф.байт(), 0x80
        out.append(bool(байт & маска))
        маска >>= 1
    return out


def _crc(буф, n):
    есть = _биты(буф, n)
    return [struct.unpack("<I", буф.байты(4))[0] if е else None for е in есть]


def _блок(буф):
    кодеров = буф.число()
    if кодеров != 1:
        raise ValueError("7z: %d кодеков в блоке (фильтры) не поддерживаются" % кодеров)
    флаг = буф.байт()
    if флаг & 0x10 or флаг & 0x80:
        raise ValueError("7z: сложный кодек не поддерживается")
    кодек = буф.байты(флаг & 0x0F)
    свойства = буф.байты(буф.число()) if флаг & 0x20 else b""
    if кодек not in (LZMA2, LZMA1):
        raise ValueError("7z: кодек %s не поддерживается (шифрование или не LZMA)"
                         % кодек.hex())
    return кодек, свойства


def _потоки(буф):
    """StreamsInfo -> {поз, упаковано, блок, распаковано, crc_блока, crc_файла}."""
    инфо = {}
    while True:
        t = буф.байт()
        if t == 0x00:
            return инфо
        if t == 0x06:                                    # PackInfo
            инфо["поз"] = буф.число()
            n = буф.число()
            if n != 1:
                raise ValueError("7z: %d упакованных потоков не поддерживаются" % n)
            while True:
                t2 = буф.байт()
                if t2 == 0x00:
                    break
                if t2 == 0x09:
                    инфо["упаковано"] = [буф.число() for _ in range(n)]
                elif t2 == 0x0A:
                    _crc(буф, n)
                else:
                    raise ValueError("7z: тег PackInfo %#x" % t2)
        elif t == 0x07:                                  # UnpackInfo
            буф.ждать(0x0B)
            блоков = буф.число()
            if блоков != 1 or буф.байт() != 0:
                raise ValueError("7z: %d блоков не поддерживаются" % блоков)
            инфо["блок"] = _блок(буф)
            буф.ждать(0x0C)
            инфо["распаковано"] = буф.число()
            t2 = буф.байт()
            if t2 == 0x0A:
                инфо["crc_блока"] = _crc(буф, 1)[0]
                t2 = буф.байт()
            if t2 != 0x00:
                raise ValueError("7z: тег UnpackInfo %#x" % t2)
        elif t == 0x08:                                  # SubStreamsInfo
            потоков = 1
            while True:
                t2 = буф.байт()
                if t2 == 0x00:
                    break
                if t2 == 0x0D:
                    потоков = буф.число()
                    if потоков != 1:
                        raise ValueError("7z: %d файлов в блоке не поддерживаются" % потоков)
                elif t2 == 0x0A:
                    инфо["crc_файла"] = _crc(буф, потоков)[0]
                else:
                    raise ValueError("7z: тег SubStreamsInfo %#x" % t2)
        else:
            raise ValueError("7z: тег StreamsInfo %#x" % t)


def _распаковщик(блок):
    кодек, св = блок
    if кодек == LZMA2:
        if len(св) != 1 or св[0] > 40:
            raise ValueError("7z: свойства LZMA2")
        p = св[0]
        словарь = 0xFFFFFFFF if p == 40 else (2 | (p & 1)) << (p // 2 + 11)
        фильтр = {"id": lzma.FILTER_LZMA2, "dict_size": словарь}
    else:
        if len(св) != 5:
            raise ValueError("7z: свойства LZMA")
        d = св[0]
        if d >= 9 * 5 * 5:
            raise ValueError("7z: свойства LZMA")
        словарь = struct.unpack("<I", св[1:5])[0]
        фильтр = {"id": lzma.FILTER_LZMA1, "lc": d % 9, "lp": (d // 9) % 5, "pb": d // 45,
                  "dict_size": словарь}
    if словарь > МАКС_СЛОВАРЬ:
        raise ValueError("7z: словарь %d МБ больше лимита" % (словарь >> 20))
    return lzma.LZMADecompressor(format=lzma.FORMAT_RAW, filters=[фильтр])


def _распаковать(fh, начало, инфо, кусок=1 << 20):
    """Генератор распакованных байтов блока с проверкой длины и CRC. Больше
    заявленного — отказ до выдачи лишнего; без CRC архив не принимается."""
    # файл в блоке один, так что CRC файла и блока — от одних и тех же байтов
    ожидаем = {инфо[к] for к in ("crc_файла", "crc_блока") if инфо.get(к) is not None}
    if not ожидаем:
        raise ValueError("7z: в архиве нет CRC — целостность не проверить")
    fh.seek(начало + инфо["поз"])
    осталось = инфо["упаковано"][0]
    д = _распаковщик(инфо["блок"])
    crc, n = 0, 0
    while осталось > 0:
        сырое = fh.read(min(кусок, осталось))
        if not сырое:
            raise ValueError("7z: архив оборван")
        осталось -= len(сырое)
        if д.eof:
            raise ValueError("7z: лишние данные после конца потока")
        выход = д.decompress(сырое, кусок)
        while выход:
            n += len(выход)
            if n > инфо["распаковано"]:
                raise ValueError("7z: распаковано больше заявленных %d байт" % инфо["распаковано"])
            crc = zlib.crc32(выход, crc)
            yield выход
            выход = b"" if д.eof or д.needs_input else д.decompress(b"", кусок)
    if n != инфо["распаковано"]:
        raise ValueError("7z: распаковано %d байт вместо %d" % (n, инфо["распаковано"]))
    # у LZMA2 есть маркер конца; у LZMA в 7z его обычно нет — там хватает длины
    if инфо["блок"][0] == LZMA2 and (not д.eof or д.unused_data):
        raise ValueError("7z: поток LZMA2 не завершён или с лишними байтами")
    if ожидаем != {crc}:
        raise ValueError("7z: CRC не сходится — архив повреждён")


def _заголовок(fh):
    """-> (начало данных, инфо потоков, имя файла)."""
    fh.seek(0)
    старт = fh.read(32)
    if len(старт) < 32 or старт[:6] != СИГНАТУРА:
        raise ValueError("7z: не 7z-архив (%r…)" % старт[:8])
    crc_старта, смещение, размер, crc_заг = struct.unpack("<IQQI", старт[8:32])
    if zlib.crc32(старт[12:32]) != crc_старта:
        raise ValueError("7z: CRC стартового заголовка не сходится")
    if размер > МАКС_ЗАГОЛОВОК:
        raise ValueError("7z: заголовок больше 64 МБ")
    fh.seek(32 + смещение)
    сырой = fh.read(размер)
    if len(сырой) != размер or zlib.crc32(сырой) != crc_заг:
        raise ValueError("7z: архив оборван (заголовок в конце не цел)")
    буф = _Буфер(сырой)
    t = буф.байт()
    if t == 0x17:                                        # сжатый заголовок
        инфо = _потоки(буф)
        if инфо.get("распаковано", 0) > МАКС_ЗАГОЛОВОК:
            raise ValueError("7z: распакованный заголовок больше 64 МБ")
        буф = _Буфер(b"".join(_распаковать(fh, 32, инфо)))
        t = буф.байт()
    if t != 0x01:
        raise ValueError("7z: тег заголовка %#x" % t)
    инфо, имя = None, None
    while True:
        t = буф.байт()
        if t == 0x00:
            break
        if t == 0x04:
            инфо = _потоки(буф)
        elif t == 0x05:
            имя = _имя(буф)
        else:
            raise ValueError("7z: тег Header %#x не поддерживается" % t)
    if not инфо or "упаковано" not in инфо:
        raise ValueError("7z: в архиве нет данных")
    return 32, инфо, имя


def _имя(буф):
    файлов = буф.число()
    if файлов != 1:
        raise ValueError("7z: %d файлов в архиве не поддерживаются" % файлов)
    имя = None
    while True:
        t = буф.байт()
        if t == 0x00:
            return имя
        размер = буф.число()
        данные = буф.байты(размер)
        if t == 0x11:
            if данные[:1] != b"\x00":
                raise ValueError("7z: имена вне заголовка не поддерживаются")
            имя = данные[1:].decode("utf-16-le").rstrip("\x00")


def оглавление(путь):
    """-> (имя файла, размер распакованного)."""
    with open(путь, "rb") as fh:
        _, инфо, имя = _заголовок(fh)
    return имя, инфо["распаковано"]


def читать(путь, кусок=1 << 20):
    """Генератор распакованных байтов единственного файла архива."""
    with open(путь, "rb") as fh:
        начало, инфо, _ = _заголовок(fh)
        yield from _распаковать(fh, начало, инфо, кусок)


class _Поток(io.RawIOBase):
    def __init__(self, куски):
        self._куски, self._буф = куски, b""

    def readable(self):
        return True

    def readinto(self, b):
        while not self._буф:
            try:
                self._буф = next(self._куски)
            except StopIteration:
                return 0
        n = min(len(b), len(self._буф))
        b[:n] = self._буф[:n]
        self._буф = self._буф[n:]
        return n

    def close(self):
        self._куски.close()
        super().close()


def открыть(путь):
    """Файлоподобный объект (bytes) над единственным файлом архива — для
    csv/io.TextIOWrapper. CRC и длина сверяются, когда поток дочитан до конца."""
    return io.BufferedReader(_Поток(читать(путь)), 1 << 20)
