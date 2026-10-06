#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
comparador.py - Compara DOS archivos: el del proyecto (nube) contra el local.

Cómo se usa: editá las rutas de la ZONA QUE EDITÁS (más abajo) y corré
    python3 comparador.py          (en Windows: python comparador.py)

Solo lee los archivos (modo binario). Nunca modifica ni escribe nada.
La última línea de la salida siempre empieza con "RESULTADO:".
"""

import bisect
import difflib
import hashlib
import itertools
import os
import re
import stat
import sys
import unicodedata
from collections import Counter
from functools import lru_cache

# =====================================================================
#                        ZONA QUE EDITÁS
#   (lo único que tenés que tocar está entre este cartel y el de abajo)
# =====================================================================

# Rutas: siempre entre comillas. Los espacios y la ñ se escriben tal cual.
#   Ubuntu:   "/home/usuario/Mis Documentos/Diseño final.md"
#   Windows:  r"C:\Users\usuario\Mis Documentos\Diseño final.md"
#             (la r antes de las comillas evita problemas con las barras \;
#              no termines la ruta con una barra \)
RUTA_PROYECTO = "/ruta/al/archivo/del/proyecto.md"
RUTA_LOCAL = "/ruta/al/archivo/local.md"

MAX_DIFERENCIAS_MOSTRADAS = 30    # cuántas diferencias se muestran como máximo
CONTEXTO_CARACTERES = 40          # caracteres de contexto a cada lado de un cambio
MAX_CARACTERES_LINEA_SOLA = 150   # cuánto se muestra de una línea que está solo en un lado
TAMANO_MAXIMO_TEXTO_MB = 10       # más grande que esto: solo se compara por hash

# =====================================================================
#            FIN DE LA ZONA QUE EDITÁS - de acá para abajo
#                      no hace falta tocar nada
# =====================================================================

# --- Ajustes internos ---
UMBRAL_SIMILITUD = 0.6             # desde qué parecido dos líneas son "la misma línea, editada"
MAX_COMPARACIONES_BLOQUE = 10000   # tope de pares de líneas a comparar por parecido en un bloque
MAX_COMPARACIONES_TOKENS = 1000000 # tope de pares de palabras a comparar dentro de una línea
MAX_COMPARACIONES_ZONA = 4000000   # tope de pares de líneas que difflib compara en una zona sin anclas
MAX_FRAGMENTOS_POR_LINEA = 3       # cuántas zonas cambiadas se muestran de una misma línea
MAX_CARACTERES_CAMBIO = 300        # largo máximo que se muestra de una zona cambiada
MIN_CARACTERES_MOVIDA = 20         # una línea más corta que esto no se considera "movida"

EXTENSIONES_BINARIAS = {
    ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".webp", ".ico",
    ".zip", ".gz", ".bz2", ".xz", ".7z", ".rar", ".tar",
    ".doc", ".docx", ".xls", ".xlsx", ".xlsm", ".ppt", ".pptx", ".odt", ".ods", ".odp",
    ".exe", ".dll", ".so", ".pyc", ".mp3", ".mp4", ".wav", ".sqlite", ".db",
}
# Los primeros bytes de un archivo binario conocido (por si le cambiaron la extensión).
FIRMAS_BINARIAS = (b"%PDF-", b"PK\x03\x04", b"\x89PNG", b"\xff\xd8\xff", b"GIF8",
                   b"\x1f\x8b", b"7z\xbc\xaf", b"Rar!", b"\xd0\xcf\x11\xe0",
                   b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")   # las dos últimas: UTF-32

SALTOS = re.compile(r"(\r\n|\n|\r)")            # los únicos saltos de línea que se reconocen
TOKENS = re.compile(r"\w+\s*|[^\w\s]\s*|\s+")   # palabra o símbolo, con su espacio de atrás
INVALIDOS = re.compile("[\udc80-\udcff]")       # bytes que no eran UTF-8 válido
ESTILOS_SALTO = {"\r\n": "CRLF", "\n": "LF", "\r": "CR"}
NOMBRES_CARACTERES = {
    "\t": "TAB", "\r": "CR", "\x1b": "ESC", "\x00": "NUL", "\xa0": "NBSP", "\u200b": "ZWSP",
    "\u200c": "ZWNJ", "\u200d": "ZWJ", "\ufeff": "BOM", "\u2028": "LS", "\u2029": "PS",
}


# =====================================================================
# Utilidades de salida
# =====================================================================

def final(texto, extra=None):
    """Imprime la última línea de la salida (siempre empieza con RESULTADO:)."""
    print("RESULTADO: " + texto + (" | " + extra if extra else ""))


def plural(n, uno, varios):
    return "%d %s" % (n, uno if n == 1 else varios)


def si_no(valor):
    return "sí" if valor else "no"


def visible(texto):
    """Muestra los caracteres invisibles o raros como <NOMBRE>. Así una diferencia
    nunca queda oculta (un NBSP se ve igual que un espacio) y un carácter de control
    del archivo no puede alterar lo que se ve en la terminal."""
    salida = []
    for c in texto:
        if c.isprintable():
            salida.append(c)
        elif "\udc80" <= c <= "\udcff":                  # byte que no era UTF-8 válido
            salida.append("<0x%02X>" % (ord(c) - 0xDC00))
        else:
            salida.append("<%s>" % NOMBRES_CARACTERES.get(c, "U+%04X" % ord(c)))
    return "".join(salida)


def recortar(texto, maximo):
    """Recorta un texto largo y avisa cuántos caracteres faltan."""
    if len(texto) <= maximo:
        return visible(texto)
    return visible(texto[:maximo]) + " ...(+%d caracteres)" % (len(texto) - maximo)


# =====================================================================
# Rutas y nombres
# =====================================================================

def resolver(ruta):
    """Revisa una ruta sin abrirla. Devuelve (estado, ruta, detalle).
    estado: 'archivo', 'carpeta', 'no_existe', 'otro' (ni archivo ni carpeta) o 'error'."""
    ruta = os.path.expanduser(ruta.strip().strip("\"'"))   # saca espacios y comillas pegadas
    try:
        modo = os.stat(ruta).st_mode
    except (FileNotFoundError, NotADirectoryError):
        # Puede que el archivo exista con las tildes en otra forma Unicode (NFC/NFD),
        # algo típico de archivos que vienen de la nube o de otro sistema.
        carpeta, nombre = os.path.split(ruta)
        try:
            for entrada in sorted(os.listdir(carpeta or ".")):
                if (entrada != nombre and unicodedata.normalize("NFC", entrada)
                        == unicodedata.normalize("NFC", nombre)):
                    otra = os.path.join(carpeta, entrada)
                    if os.path.isfile(otra):
                        return "archivo", otra, ("no encontré '%s' tal cual, pero sí un archivo con las "
                                                 "tildes en otra forma Unicode (NFC/NFD); uso '%s'"
                                                 % (nombre, entrada))
        except OSError:
            pass
        return "no_existe", ruta, None
    except OSError as e:                                  # permisos, ruta inválida, etc.
        return "error", ruta, e.strerror or str(e)
    if stat.S_ISDIR(modo):
        return "carpeta", ruta, None
    if stat.S_ISREG(modo):
        return "archivo", ruta, None
    return "otro", ruta, None


def simplificar(nombre):
    """Nombre sin tildes ni mayúsculas, para ver si dos nombres son 'el mismo'."""
    d = unicodedata.normalize("NFD", nombre)
    return "".join(c for c in d if not unicodedata.combining(c)).casefold()


def revisar_nombres(ruta_p, ruta_l):
    """Compara solo el nombre de archivo (no la carpeta). Devuelve un aviso o None."""
    np_, nl = os.path.basename(ruta_p), os.path.basename(ruta_l)
    if np_ == nl:
        return None
    if unicodedata.normalize("NFC", np_) == unicodedata.normalize("NFC", nl):
        return "los nombres solo difieren en la forma Unicode de las tildes (NFC/NFD)"
    if simplificar(np_) == simplificar(nl):
        return "MISMO ARCHIVO, OTRO NOMBRE (%s / %s)" % (np_, nl)
    return ("los nombres no coinciden, verificá que sean los archivos correspondientes (%s / %s)"
            % (np_, nl))


# =====================================================================
# Lectura (solo lectura, modo binario)
# =====================================================================

def leer(ruta, limite):
    """Lee el archivo por bloques. Devuelve (tamaño, sha256, datos).
    Si pasa el límite de tamaño, solo calcula el hash y datos es None."""
    sha = hashlib.sha256()
    trozos, total = [], 0
    with open(ruta, "rb") as f:
        while True:
            trozo = f.read(1 << 20)
            if not trozo:
                break
            sha.update(trozo)
            total += len(trozo)
            if trozos is not None:
                if total > limite:
                    trozos = None            # demasiado grande: se descartan los datos
                else:
                    trozos.append(trozo)
    return total, sha.hexdigest(), (b"".join(trozos) if trozos is not None else None)


def parece_binario(nombre, datos):
    """Binario = extensión conocida, firma conocida o bytes nulos
    (salvo UTF-16 con BOM, que es texto aunque tenga bytes nulos)."""
    if os.path.splitext(nombre)[1].lower() in EXTENSIONES_BINARIAS:
        return True
    if datos.startswith(FIRMAS_BINARIAS):
        return True
    if datos.startswith((b"\xff\xfe", b"\xfe\xff")):
        return False
    return b"\x00" in datos


class Archivo:
    """Un archivo ya leído. Si es texto, guarda también sus líneas."""

    def __init__(self, etiqueta, ruta):
        self.etiqueta = etiqueta                      # "Proyecto" o "Local"
        self.ruta = os.path.abspath(ruta)
        self.nombre = os.path.basename(ruta)
        limite = int(TAMANO_MAXIMO_TEXTO_MB * 1024 * 1024)
        self.tamano, self.sha, self.datos = leer(ruta, limite)
        self.grande = self.datos is None              # pasó el límite: solo se compara el hash
        self.binario = False
        self.n_lineas = None
        self.invalidos = 0
        if not self.grande:
            self.binario = parece_binario(self.nombre, self.datos)
            if not self.binario:
                self.cargar_texto()

    def cargar_texto(self):
        """Decodifica y parte en líneas. Las líneas se cortan solo en \\n, \\r\\n o \\r
        (como un editor), así los números de línea coinciden con los que ves vos."""
        datos = self.datos
        self.bom = False
        if datos.startswith((b"\xff\xfe", b"\xfe\xff")):
            self.codificacion = "UTF-16 " + ("LE" if datos[:2] == b"\xff\xfe" else "BE")
            try:
                texto = datos.decode("utf-16")        # el BOM lo saca el decodificador
            except UnicodeDecodeError:
                self.binario = True                   # UTF-16 roto: se trata como binario
                return
        else:
            self.codificacion = "UTF-8"
            self.bom = datos.startswith(b"\xef\xbb\xbf")
            # surrogateescape guarda cada byte inválido como un carácter propio: dos archivos
            # con bytes inválidos distintos nunca pueden parecer iguales (errors="replace" sí).
            texto = (datos[3:] if self.bom else datos).decode("utf-8", errors="surrogateescape")
        if not texto.isascii():
            self.invalidos = sum(1 for _ in INVALIDOS.finditer(texto))
        piezas = SALTOS.split(texto)                  # [línea, salto, línea, salto, ..., línea]
        self.contenidos, self.terminadores = piezas[0::2], piezas[1::2]
        # Tras el último salto solo puede quedar nada o espacios/tabs finales: no es otra línea.
        self.termina_en_salto = len(self.contenidos) > 1 and self.contenidos[-1].strip(" \t") == ""
        self.n_lineas = len(self.contenidos) - (1 if self.contenidos[-1] == "" else 0)

    def descripcion(self):
        partes = ["%d bytes" % self.tamano]
        if self.n_lineas is not None:
            partes.append(plural(self.n_lineas, "línea", "líneas"))
        partes.append("sha256 " + self.sha[:12])
        return "%-10s %s (%s)\n%-10s ruta: %s" % (self.etiqueta + ":", self.nombre,
                                                  ", ".join(partes), "", self.ruta)


def normalizar(doc):
    """Líneas del texto sin las diferencias de formato que se ignoran:
    saltos de línea (ya separados), espacios/tabs al final de línea, UN salto final
    y forma Unicode (NFC). El BOM ya se sacó al decodificar."""
    lineas = [unicodedata.normalize("NFC", c.rstrip(" \t")) for c in doc.contenidos]
    if doc.termina_en_salto:
        lineas.pop()
    return lineas


# =====================================================================
# Comparación línea por línea
# =====================================================================

def subsecuencia_creciente(pares):
    """De una lista de pares (i, j) ordenada por i, la subsecuencia más larga con j creciente."""
    cola_j, cola_n, previo = [], [], []
    for n, (_, j) in enumerate(pares):
        k = bisect.bisect_left(cola_j, j)
        if k == len(cola_j):
            cola_j.append(j)
            cola_n.append(n)
        else:
            cola_j[k], cola_n[k] = j, n
        previo.append(cola_n[k - 1] if k else None)
    res, n = [], (cola_n[-1] if cola_n else None)
    while n is not None:
        res.append(pares[n])
        n = previo[n]
    return res[::-1]


def alinear(a, b):
    """Opcodes (equal / replace / delete / insert) entre dos listas de líneas, y si alguna
    zona quedó resuelta de forma aproximada. Método 'paciencia' (el de git): se ancla en las
    líneas que aparecen UNA sola vez en cada lado, así es rápido aunque el archivo sea enorme
    y tenga muchas ediciones. Lo que queda entre anclas se resuelve con difflib
    (autojunk=False: con el valor por defecto difflib trata como 'basura' las líneas
    frecuentes y alinea mal). Lo que se marca 'equal' siempre es igual de verdad."""
    ops, aproximado = [], False
    pendientes = [("rango", 0, len(a), 0, len(b))]       # pila: se expande de a un rango
    while pendientes:
        item = pendientes.pop()
        if item[0] == "op":
            ops.append(item[1:])
            continue
        _, alo, ahi, blo, bhi = item
        trozos = []                                       # lo que sale de este rango, en orden
        ini = 0
        while alo + ini < ahi and blo + ini < bhi and a[alo + ini] == b[blo + ini]:
            ini += 1
        fin = 0
        while fin < ahi - alo - ini and fin < bhi - blo - ini and a[ahi - 1 - fin] == b[bhi - 1 - fin]:
            fin += 1
        if ini:
            trozos.append(("op", "equal", alo, alo + ini, blo, blo + ini))
        alo, ahi, blo, bhi = alo + ini, ahi - fin, blo + ini, bhi - fin
        if alo == ahi and blo == bhi:
            pass
        elif alo == ahi:
            trozos.append(("op", "insert", alo, alo, blo, bhi))
        elif blo == bhi:
            trozos.append(("op", "delete", alo, ahi, blo, blo))
        else:
            cuenta_a, cuenta_b = Counter(a[alo:ahi]), Counter(b[blo:bhi])
            pos_b = {b[j]: j for j in range(blo, bhi) if cuenta_b[b[j]] == 1 and cuenta_a.get(b[j]) == 1}
            anclas = subsecuencia_creciente([(i, pos_b[a[i]]) for i in range(alo, ahi) if a[i] in pos_b])
            if anclas:
                ia, ib = alo, blo
                for i, j in anclas:
                    trozos.append(("rango", ia, i, ib, j))
                    trozos.append(("op", "equal", i, i + 1, j, j + 1))
                    ia, ib = i + 1, j + 1
                trozos.append(("rango", ia, ahi, ib, bhi))
            elif (ahi - alo) * (bhi - blo) <= MAX_COMPARACIONES_ZONA:
                medio = difflib.SequenceMatcher(None, a[alo:ahi], b[blo:bhi], autojunk=False)
                trozos.extend(("op", t, i1 + alo, i2 + alo, j1 + blo, j2 + blo)
                              for t, i1, i2, j1, j2 in medio.get_opcodes())
            else:
                trozos.append(("op", "replace", alo, ahi, blo, bhi))
                aproximado = True
        if fin:
            trozos.append(("op", "equal", ahi, ahi + fin, bhi, bhi + fin))
        pendientes.extend(reversed([t for t in trozos if t[0] == "op" or t[1] < t[2] or t[3] < t[4]]))
    return juntar(ops), aproximado


def juntar(ops):
    """Une opcodes vecinos del mismo tipo y junta los cambios seguidos en uno solo."""
    res = []
    for tag, i1, i2, j1, j2 in ops:
        if res and (res[-1][0] == "equal") == (tag == "equal"):
            t, a1, _, b1, _ = res[-1]
            res[-1] = (t if t == tag else "replace", a1, i2, b1, j2)
        else:
            res.append((tag, i1, i2, j1, j2))
    return [(("replace" if i2 > i1 and j2 > j1 else "delete" if i2 > i1 else "insert") if t != "equal" else t,
             i1, i2, j1, j2) for t, i1, i2, j1, j2 in res]


@lru_cache(maxsize=4096)
def palabras(linea):
    return frozenset(re.findall(r"\w+", linea))


def similitud(x, y):
    """Parecido entre dos líneas, de 0 a 1."""
    if len(x) <= 300 and len(y) <= 300:
        s = difflib.SequenceMatcher(None, x, y, autojunk=False)
        if s.real_quick_ratio() < UMBRAL_SIMILITUD or s.quick_ratio() < UMBRAL_SIMILITUD:
            return 0.0
        return s.ratio()
    px, py = palabras(x), palabras(y)                 # líneas largas: parecido por palabras
    return len(px & py) / len(px | py) if (px or py) else 0.0


def emparejar(a, b, i1, i2, j1, j2):
    """En un bloque que cambió, decide qué línea del proyecto es la versión editada de
    qué línea local (por parecido, respetando el orden). Las que no se parecen a ninguna
    quedan como 'solo en un lado'. Evita mostrar como 'cambió' un párrafo reescrito entero
    o emparejar mal cuando se agregó una línea al lado de una editada."""
    todas = (i2 - i1) * (j2 - j1) <= MAX_COMPARACIONES_BLOQUE
    res, j = [], j1
    for i in range(i1, i2):
        mejor, mejor_s = None, 0.0
        for k in (range(j, j2) if todas else range(j, min(j + 1, j2))):
            if a[i] == b[k]:
                continue                              # idénticas: no es una edición (ver 'movida')
            s = similitud(a[i], b[k])
            if s >= UMBRAL_SIMILITUD and s > mejor_s:
                mejor, mejor_s = k, s
        if mejor is None:
            res.append(("solo_proy", i))
        else:
            res.extend(("solo_local", k) for k in range(j, mejor))
            res.append(("cambio", i, mejor))
            j = mejor + 1
    res.extend(("solo_local", k) for k in range(j, j2))
    return res


def marcar_movidas(items, a, b):
    """Una línea 'solo en proyecto' idéntica a una 'solo en local' es un párrafo movido de lugar."""
    esperando = {}
    for n, it in enumerate(items):
        if it[0] == "solo_local" and len(b[it[1]].strip()) >= MIN_CARACTERES_MOVIDA:
            esperando.setdefault(b[it[1]], []).append(n)
    pares, usados = {}, set()
    for n, it in enumerate(items):
        if it[0] == "solo_proy" and esperando.get(a[it[1]]):
            m = esperando[a[it[1]]].pop(0)
            pares[n] = m
            usados.add(m)
    nuevos = []
    for n, it in enumerate(items):
        if n in usados:
            continue
        nuevos.append(("movida", it[1], items[pares[n]][1]) if n in pares else it)
    return nuevos


def clasificar(a, b):
    """Compara dos listas de líneas. Devuelve (diferencias, pares, aproximado):
    diferencias = lista de ('cambio', i, j) | ('solo_proy', i) | ('solo_local', j) | ('movida', i, j)
    pares = (i, j) de las líneas que coinciden."""
    items, pares = [], []
    ops, aproximado = alinear(a, b)
    for tag, i1, i2, j1, j2 in ops:
        if tag == "equal":
            pares.extend(zip(range(i1, i2), range(j1, j2)))
        elif tag == "delete":
            items.extend(("solo_proy", i) for i in range(i1, i2))
        elif tag == "insert":
            items.extend(("solo_local", j) for j in range(j1, j2))
        else:
            items.extend(emparejar(a, b, i1, i2, j1, j2))
    return marcar_movidas(items, a, b), pares, aproximado


# =====================================================================
# Diferencias dentro de una línea
# =====================================================================

def es_palabra(c):
    return c.isalnum() or c == "_"


def grupos_de_cambios(x, y):
    """Zonas que cambiaron entre dos líneas. Devuelve una lista de grupos; cada grupo es
    una lista de zonas (ini_x, fin_x, ini_y, fin_y) en caracteres. Las zonas que quedan
    a menos de 2*CONTEXTO_CARACTERES se juntan en un solo grupo."""
    # 1) Recortar lo común al inicio y al final (rápido: casi siempre aísla el cambio).
    tope = min(len(x), len(y))
    p = 0
    while p < tope and x[p] == y[p]:
        p += 1
    s = 0
    while s < tope - p and x[-1 - s] == y[-1 - s]:
        s += 1
    ta, tb = TOKENS.findall(x[p:len(x) - s]), TOKENS.findall(y[p:len(y) - s])
    # 2) Dentro de lo que queda, comparar por palabras (más legible que por letra suelta).
    if len(ta) * len(tb) > MAX_COMPARACIONES_TOKENS:
        zonas = [(p, len(x) - s, p, len(y) - s)]      # demasiado grande: una sola zona
    else:
        ox = list(itertools.accumulate(map(len, ta), initial=0))
        oy = list(itertools.accumulate(map(len, tb), initial=0))
        zonas = [(p + ox[i1], p + ox[i2], p + oy[j1], p + oy[j2])
                 for t, i1, i2, j1, j2 in
                 difflib.SequenceMatcher(None, ta, tb, autojunk=False).get_opcodes() if t != "equal"]
    # 3) Sacar lo que ambas versiones tienen igual en los bordes de la zona (ej. un espacio
    #    pegado a la palabra) y, si el cambio cae en medio de una palabra, mostrarla entera.
    ampliadas = []
    for sx, ex, sy, ey in zonas:
        while sx < ex and sy < ey and x[sx] == y[sy]:
            sx, sy = sx + 1, sy + 1
        while sx < ex and sy < ey and x[ex - 1] == y[ey - 1]:
            ex, ey = ex - 1, ey - 1
        if sx == ex and sy == ey:
            continue
        while (sx > 0 and sy > 0 and x[sx - 1] == y[sy - 1] and es_palabra(x[sx - 1])
               and ((sx < ex and es_palabra(x[sx])) or (sy < ey and es_palabra(y[sy])))):
            sx, sy = sx - 1, sy - 1
        while (ex < len(x) and ey < len(y) and x[ex] == y[ey] and es_palabra(x[ex])
               and ((sx < ex and es_palabra(x[ex - 1])) or (sy < ey and es_palabra(y[ey - 1])))):
            ex, ey = ex + 1, ey + 1
        ampliadas.append((sx, ex, sy, ey))
    # 4) Juntar zonas que se pisan o quedan cerca.
    grupos = []
    for z in sorted(ampliadas):
        if grupos:
            ult = grupos[-1][-1]
            hueco = z[0] - ult[1]
            if hueco <= 0:
                grupos[-1][-1] = (ult[0], max(ult[1], z[1]), ult[2], max(ult[3], z[3]))
                continue
            if hueco <= 2 * CONTEXTO_CARACTERES:
                grupos[-1].append(z)
                continue
        grupos.append([z])
    return grupos


def pintar(texto, zonas):
    """Fragmento de una línea: contexto + zonas cambiadas marcadas con [[ ]] + contexto."""
    desde = max(0, zonas[0][0] - CONTEXTO_CARACTERES)
    hasta = min(len(texto), zonas[-1][1] + CONTEXTO_CARACTERES)
    salida = "..." if desde > 0 else ""
    pos = desde
    for ini, fin in zonas:
        salida += visible(texto[pos:ini]) + "[[" + recortar(texto[ini:fin], MAX_CARACTERES_CAMBIO) + "]]"
        pos = fin
    return salida + visible(texto[pos:hasta]) + ("..." if hasta < len(texto) else "")


def mostrar(n, total, item, a, b):
    """Imprime una diferencia (a y b son las líneas normalizadas de proyecto y local)."""
    print("Diferencia %d de %d" % (n, total))
    tipo = item[0]
    if tipo == "cambio":
        _, i, j = item
        grupos = grupos_de_cambios(a[i], b[j])
        for g in grupos[:MAX_FRAGMENTOS_POR_LINEA]:
            en_proyecto = pintar(a[i], [(z[0], z[1]) for z in g])
            en_local = pintar(b[j], [(z[2], z[3]) for z in g])
            print("  Proyecto,  línea %d, col. %d: %s" % (i + 1, g[0][0] + 1, en_proyecto))
            print("  Local,     línea %d, col. %d: %s" % (j + 1, g[0][2] + 1, en_local))
        if len(grupos) > MAX_FRAGMENTOS_POR_LINEA:
            print("  (+%s más en esta línea)" % plural(len(grupos) - MAX_FRAGMENTOS_POR_LINEA,
                                                       "cambio", "cambios"))
    elif tipo == "solo_proy":
        print("  SOLO EN PROYECTO, línea %d: %s" % (item[1] + 1, linea_sola(a[item[1]])))
    elif tipo == "solo_local":
        print("  SOLO EN LOCAL, línea %d: %s" % (item[1] + 1, linea_sola(b[item[1]])))
    else:
        print("  MOVIDA de lugar: línea %d en proyecto -> línea %d en local: %s"
              % (item[1] + 1, item[2] + 1, linea_sola(a[item[1]])))


def linea_sola(linea):
    return recortar(linea, MAX_CARACTERES_LINEA_SOLA) if linea else "(línea vacía)"


# =====================================================================
# Diferencias de formato
# =====================================================================

def estilo_saltos(doc):
    cuenta = Counter(ESTILOS_SALTO[t] for t in doc.terminadores)
    if not cuenta:
        return "sin saltos"
    if len(cuenta) == 1:
        return next(iter(cuenta))
    return "mixto (" + ", ".join("%s %d" % kv for kv in cuenta.most_common()) + ")"


def diferencias_de_formato(p, l, pares):
    """Qué diferencias de formato hay entre dos textos. Se miran las líneas que coinciden una
    vez normalizadas, así que sirve tanto para 'igual salvo formato' como para 'no igual'."""
    saltos = espacios = unicode_ = unicode_p = unicode_l = 0
    for i, j in pares:
        cp, cl = p.contenidos[i], l.contenidos[j]
        if cp != cl:
            sp, sl = cp.rstrip(" \t"), cl.rstrip(" \t")
            if cp[len(sp):] != cl[len(sl):]:
                espacios += 1
            if sp != sl:                              # difieren solo en la forma Unicode
                unicode_ += 1
                unicode_p += sp != unicodedata.normalize("NFC", sp)
                unicode_l += sl != unicodedata.normalize("NFC", sl)
        if (i < len(p.terminadores) and j < len(l.terminadores)
                and p.terminadores[i] != l.terminadores[j]):
            saltos += 1
    if (p.contenidos[-1] if p.termina_en_salto else "") != (l.contenidos[-1] if l.termina_en_salto else ""):
        espacios += 1                                 # espacios/tabs después del último salto
    desc = []
    if saltos:
        desc.append("saltos de línea: proyecto %s, local %s" % (estilo_saltos(p), estilo_saltos(l)))
    if espacios:
        desc.append("espacios o tabs al final de línea: en %s" % plural(espacios, "línea", "líneas"))
    if p.termina_en_salto != l.termina_en_salto:
        desc.append("salto de línea al final del archivo: proyecto %s, local %s"
                    % (si_no(p.termina_en_salto), si_no(l.termina_en_salto)))
    if p.bom != l.bom:
        desc.append("BOM al inicio: proyecto %s, local %s" % (si_no(p.bom), si_no(l.bom)))
    if unicode_:
        forma = ("proyecto en NFD, local en NFC" if unicode_p and not unicode_l else
                 "proyecto en NFC, local en NFD" if unicode_l and not unicode_p else
                 "mezcla de NFC y NFD")
        desc.append("tildes y ñ en otra forma Unicode: %s, en %s" % (forma, plural(unicode_, "línea", "líneas")))
    if p.codificacion != l.codificacion:
        desc.append("codificación: proyecto %s, local %s" % (p.codificacion, l.codificacion))
    return desc


# =====================================================================
# Flujo principal
# =====================================================================

def comparar_textos(p, l, nombres):
    """Compara dos archivos de texto cuyos bytes son distintos."""
    a, b = normalizar(p), normalizar(l)
    items, pares, aproximado = clasificar(a, b)
    # Un archivo realmente vacío no tiene ninguna línea (ni siquiera una vacía).
    items = [it for it in items if not (it == ("solo_proy", 0) and p.n_lineas == 0)
             and not (it == ("solo_local", 0) and l.n_lineas == 0)]
    formato = diferencias_de_formato(p, l, pares)
    if not items:
        if formato:
            return final("IGUAL SALVO FORMATO (" + "; ".join(formato) + ")", nombres)
        return final("NO IGUAL (los bytes difieren pero no pude identificar la causa; "
                     "revisalos con otra herramienta)", nombres)
    if formato:
        print("Formato: además hay diferencias de formato, que NO se cuentan como cambios: "
              + "; ".join(formato))
    if aproximado:
        print("AVISO: hay una zona muy grande y repetitiva que no pude alinear con precisión; "
              "las diferencias de esa zona pueden estar infladas (las líneas listadas sí difieren).")
    total = len(items)
    for n, item in enumerate(items[:MAX_DIFERENCIAS_MOSTRADAS], 1):
        mostrar(n, total, item, a, b)
    if total > MAX_DIFERENCIAS_MOSTRADAS:
        print("... Faltan %d diferencias por mostrar (se muestran %d de %d). "
              "Subí MAX_DIFERENCIAS_MOSTRADAS para verlas."
              % (total - MAX_DIFERENCIAS_MOSTRADAS, max(MAX_DIFERENCIAS_MOSTRADAS, 0), total))
    c = Counter(it[0] for it in items)
    detalle = "%s, %d solo en proyecto, %d solo en local" % (
        plural(c["cambio"], "línea distinta", "líneas distintas"), c["solo_proy"], c["solo_local"])
    if c["movida"]:
        detalle += ", " + plural(c["movida"], "línea movida", "líneas movidas")
    final("NO IGUAL (" + detalle + ")", nombres)


def comparar(ruta_proyecto, ruta_local):
    """Compara los dos archivos, imprime el informe y termina con la línea RESULTADO."""
    rp, rl = resolver(ruta_proyecto), resolver(ruta_local)
    # Casos en los que no hay nada que comparar.
    for etiqueta, (estado, ruta, detalle) in (("Proyecto", rp), ("Local", rl)):
        motivo = {"carpeta": "es una carpeta (este programa compara de a un archivo)",
                  "otro": "no es un archivo común",
                  "error": "no se pudo acceder: %s" % detalle}.get(estado)
        if motivo:
            print("%s: %s" % (etiqueta, ruta))
            return final("ERROR (la ruta de %s %s)" % (etiqueta.lower(), motivo))
    (est_p, ruta_p, aviso_p), (est_l, ruta_l, aviso_l) = rp, rl
    for aviso in (aviso_p, aviso_l):
        if aviso:
            print("AVISO: " + aviso)
    nombres = revisar_nombres(ruta_p, ruta_l)
    if est_p != "archivo" or est_l != "archivo":
        for etiqueta, estado, ruta in (("Proyecto", est_p, ruta_p), ("Local", est_l, ruta_l)):
            if estado == "archivo":
                try:
                    print(Archivo(etiqueta, ruta).descripcion())
                except OSError as e:
                    return final("ERROR (no se pudo leer %s: %s)" % (ruta, e.strerror or e))
            else:
                print("%-10s no existe: %s" % (etiqueta + ":", ruta))
        if est_p != "archivo" and est_l != "archivo":
            return final("NO EXISTE NINGUNO DE LOS DOS (fallaron las dos rutas; "
                         "revisá la zona que editás)", nombres)
        if est_l == "archivo":
            return final("SOLO LOCAL (no existe el archivo del proyecto en la ruta indicada)", nombres)
        return final("SOLO PROYECTO (no existe el archivo local en la ruta indicada)", nombres)
    if os.path.samefile(ruta_p, ruta_l):
        return final("ERROR (las dos rutas apuntan al mismo archivo: no hay nada que comparar)")

    try:
        p, l = Archivo("Proyecto", ruta_p), Archivo("Local", ruta_l)
    except OSError as e:
        return final("ERROR (no se pudo leer un archivo: %s)" % (e.strerror or e))
    print(p.descripcion())
    print(l.descripcion())
    for doc in (p, l):
        if doc.invalidos:
            print("AVISO: %s: %s no son UTF-8 válido; se comparan tal cual y en pantalla "
                  "se ven como <0xNN>." % (doc.etiqueta, plural(doc.invalidos, "byte", "bytes")))

    if p.sha == l.sha:
        return final("IGUAL", nombres)
    if p.grande or l.grande:
        print("AVISO: algún archivo supera los %s MB: no se analiza el contenido, solo el hash. "
              "No puedo mostrar qué cambió (subí TAMANO_MAXIMO_TEXTO_MB si querés verlo)."
              % TAMANO_MAXIMO_TEXTO_MB)
        return final("NO IGUAL (archivos grandes: solo se comparó el hash)", nombres)
    if p.binario or l.binario:
        print("AVISO: archivo binario (PDF, imagen, comprimido, Office...): no se puede mostrar "
              "qué cambió adentro. NO IGUAL significa que los bytes difieren; en un PDF o en un "
              "documento de Office puede ser solo metadatos (fechas, identificadores) aunque se vea igual.")
        tam = ("mismo tamaño" if p.tamano == l.tamano
               else "tamaños distintos: %d contra %d bytes" % (p.tamano, l.tamano))
        return final("NO IGUAL (binarios, %s)" % tam, nombres)
    comparar_textos(p, l, nombres)


def main():
    try:
        sys.stdout.reconfigure(errors="replace")      # que un carácter raro nunca rompa la salida
    except (AttributeError, ValueError):
        pass
    try:
        comparar(RUTA_PROYECTO, RUTA_LOCAL)
    except (Exception, KeyboardInterrupt) as e:       # la última línea siempre es RESULTADO
        final("ERROR (%s: %s)" % (type(e).__name__, e))


if __name__ == "__main__":
    main()
