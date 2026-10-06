# -*- coding: utf-8 -*-
"""
Pruebas de comparador.py. Se corren con:   python3 -m unittest -v test_comparador

Cada prueba crea sus archivos en una carpeta temporal que se borra sola:
no queda nada en el disco. Sirven para comprobar, después de tocar el programa,
que sigue diciendo la verdad.
"""

import contextlib
import hashlib
import io
import os
import random
import sys
import tempfile
import time
import unicodedata
import unittest
from unittest import mock

import comparador

# --- Espía de escrituras: registra cualquier intento de escribir/borrar/renombrar ---
_ESPIANDO = False
_ESCRITURAS = []
_EVENTOS_ESCRITURA = ("os.remove", "os.rename", "os.mkdir", "os.rmdir", "os.truncate",
                      "os.chmod", "os.utime", "os.symlink", "os.link", "shutil.copyfile")


def _gancho(evento, args):
    if not _ESPIANDO:
        return
    if evento == "open":
        modo = args[1] if len(args) > 1 else None
        flags = args[2] if len(args) > 2 and isinstance(args[2], int) else 0
        escribe = bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC))
        if isinstance(modo, str) and any(c in modo for c in "wax+"):
            escribe = True
        if escribe:
            _ESCRITURAS.append((evento, args[0], modo, flags))
    elif evento in _EVENTOS_ESCRITURA:
        _ESCRITURAS.append((evento, args))


sys.addaudithook(_gancho)


def lineas_unicas(n, semilla=1, palabras=10):
    """n líneas distintas entre sí (vocabulario grande, así ninguna se parece a otra)."""
    azar = random.Random(semilla)
    return ["%s." % " ".join("w%d" % azar.randrange(100000) for _ in range(palabras)) for _ in range(n)]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        # Valores fijos: que las pruebas no dependan de lo que edites en la zona del programa.
        parche = mock.patch.multiple(comparador, CONTEXTO_CARACTERES=40, MAX_DIFERENCIAS_MOSTRADAS=30,
                                     MAX_CARACTERES_LINEA_SOLA=150, TAMANO_MAXIMO_TEXTO_MB=10)
        parche.start()
        self.addCleanup(parche.stop)

    def archivo(self, nombre, datos, carpeta="x"):
        ruta = os.path.join(self.dir, carpeta, nombre)
        os.makedirs(os.path.dirname(ruta), exist_ok=True)
        with open(ruta, "wb") as f:
            f.write(datos)
        return ruta

    def correr(self, ruta_p, ruta_l):
        """Corre la comparación. Devuelve (salida completa, última línea)."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            comparador.comparar(ruta_p, ruta_l)
        salida = buf.getvalue()
        ultima = salida.rstrip("\n").split("\n")[-1]
        self.assertTrue(ultima.startswith("RESULTADO: "), "la última línea no es RESULTADO: %r" % ultima)
        return salida, ultima

    def bytes_(self, bp, bl, nombre_p="doc.md", nombre_l="doc.md"):
        return self.correr(self.archivo(nombre_p, bp, "proyecto"), self.archivo(nombre_l, bl, "local"))

    def texto(self, tp, tl, **kw):
        return self.bytes_(tp.encode("utf-8"), tl.encode("utf-8"), **kw)


# =====================================================================
# Las 9 pruebas pedidas
# =====================================================================
class PruebasPedidas(Base):
    def test_01_identicos(self):
        contenido = b"hola\nmundo\n"
        salida, ultima = self.bytes_(contenido, contenido)
        self.assertEqual(ultima, "RESULTADO: IGUAL")
        self.assertIn("11 bytes", salida)
        self.assertIn("2 líneas", salida)
        self.assertEqual(salida.count(hashlib.sha256(contenido).hexdigest()[:12]), 2)

    def test_02_palabra_cambiada_en_parrafo_largo(self):
        linea = " ".join("alfa%d" % i for i in range(3000))              # ~21.000 caracteres
        nueva = linea.replace("alfa1500", "ZETA1500", 1)
        salida, ultima = self.texto("Titulo\n" + linea + "\nFin\n", "Titulo\n" + nueva + "\nFin\n")
        self.assertEqual(ultima, "RESULTADO: NO IGUAL (1 línea distinta, 0 solo en proyecto, 0 solo en local)")
        self.assertIn("[[alfa1500]]", salida)
        self.assertIn("[[ZETA1500]]", salida)
        self.assertIn("línea 2, col. %d" % (linea.index("alfa1500") + 1), salida)
        self.assertLess(len(salida), 1500)                                # no se imprimió la línea entera
        self.assertNotIn("alfa10 ", salida)
        # Contexto: exactamente 40 caracteres a cada lado.
        previo = linea[:linea.index("alfa1500")][-40:]
        posterior = linea[linea.index("alfa1500") + len("alfa1500"):][:40]
        self.assertIn("..." + previo + "[[alfa1500]]" + posterior + "...", salida)

    def test_03_crlf_contra_lf(self):
        salida, ultima = self.texto("uno\r\ndos\r\ntres\r\n", "uno\ndos\ntres\n")
        self.assertTrue(ultima.startswith("RESULTADO: IGUAL SALVO FORMATO"))
        self.assertIn("saltos de línea: proyecto CRLF, local LF", ultima)

    def test_04_ñ_en_nfc_contra_nfd(self):
        nfc = "Diseño y canción\n"
        nfd = unicodedata.normalize("NFD", nfc)
        self.assertNotEqual(nfc, nfd)
        salida, ultima = self.texto(nfc, nfd)
        self.assertTrue(ultima.startswith("RESULTADO: IGUAL SALVO FORMATO"))
        self.assertIn("Unicode", ultima)
        self.assertIn("proyecto en NFC, local en NFD", ultima)

    def test_05_linea_agregada_y_borrada(self):
        base = lineas_unicas(4)
        nueva = "Esta es una línea totalmente distinta y nueva."
        salida, ultima = self.texto("\n".join(base) + "\n", "\n".join([base[0], base[2], base[3], nueva]) + "\n")
        self.assertEqual(ultima, "RESULTADO: NO IGUAL (0 líneas distintas, 1 solo en proyecto, 1 solo en local)")
        self.assertIn("SOLO EN PROYECTO, línea 2: " + base[1], salida)
        self.assertIn("SOLO EN LOCAL, línea 4: " + nueva, salida)

    def test_06_faltan_archivos(self):
        existe = self.archivo("a.md", b"hola\n")
        falta_p = os.path.join(self.dir, "no", "esta", "a.md")
        falta_l = os.path.join(self.dir, "tampoco", "a.md")
        _, ultima = self.correr(falta_p, existe)
        self.assertTrue(ultima.startswith("RESULTADO: SOLO LOCAL (no existe el archivo del proyecto"))
        _, ultima = self.correr(existe, falta_l)
        self.assertTrue(ultima.startswith("RESULTADO: SOLO PROYECTO (no existe el archivo local"))
        salida, ultima = self.correr(falta_p, falta_l)
        self.assertTrue(ultima.startswith("RESULTADO: NO EXISTE NINGUNO DE LOS DOS"))
        self.assertIn(falta_p, salida)
        self.assertIn(falta_l, salida)

    def test_07_nombres_con_y_sin_tilde(self):
        salida, ultima = self.texto("igual\n", "igual\n", nombre_p="Diseño_X.md", nombre_l="Diseno_X.md")
        self.assertTrue(ultima.startswith("RESULTADO: IGUAL | MISMO ARCHIVO, OTRO NOMBRE (Diseño_X.md / Diseno_X.md)"))
        # También con otra capitalización, y el contenido igual se compara.
        _, ultima = self.texto("a\n", "b\n", nombre_p="DISEÑO_x.MD", nombre_l="diseno_X.md")
        self.assertIn("MISMO ARCHIVO, OTRO NOMBRE", ultima)
        self.assertIn("NO IGUAL", ultima)
        # Nombres sin relación: otro aviso, y compara igual.
        _, ultima = self.texto("a\n", "a\n", nombre_p="Informe.md", nombre_l="Otro.md")
        self.assertIn("los nombres no coinciden, verificá que sean los archivos correspondientes", ultima)
        self.assertTrue(ultima.startswith("RESULTADO: IGUAL | "))

    def test_08_dos_pdf_falsos_distintos(self):
        pdf1 = b"%PDF-1.4\n1 0 obj\n<< /Creado (2026-07-25) >>\nendobj\n%%EOF\n"
        pdf2 = b"%PDF-1.4\n1 0 obj\n<< /Creado (2026-07-26) >>\nendobj\n%%EOF\n"
        salida, ultima = self.bytes_(pdf1, pdf2, "informe.pdf", "informe.pdf")
        self.assertTrue(ultima.startswith("RESULTADO: NO IGUAL (binarios, mismo tamaño)"))
        self.assertIn("no se puede mostrar qué cambió", salida)
        self.assertIn("%d bytes" % len(pdf1), salida)
        _, ultima = self.bytes_(pdf1, pdf1, "informe.pdf", "informe.pdf")
        self.assertEqual(ultima, "RESULTADO: IGUAL")
        _, ultima = self.bytes_(pdf1, pdf2 + b"extra", "informe.pdf", "informe.pdf")
        self.assertIn("tamaños distintos: %d contra %d bytes" % (len(pdf1), len(pdf2) + 5), ultima)

    def test_09_xlsx_es_binario_y_solo_se_hashea(self):
        x1 = b"PK\x03\x04" + b"\x00\x01" * 50 + b"datos personales: Ana, 12345678"
        x2 = b"PK\x03\x04" + b"\x00\x01" * 50 + b"datos personales: Beto, 8765432"
        salida, ultima = self.bytes_(x1, x2, "datos.xlsx", "datos.xlsx")
        self.assertTrue(ultima.startswith("RESULTADO: NO IGUAL (binarios"))
        self.assertNotIn("Ana", salida)                                    # nunca se muestra su contenido
        self.assertNotIn("BLOQUEADO", salida)                              # ya no hay bloqueo por privacidad
        _, ultima = self.bytes_(x1, x1, "datos.xlsx", "datos.xlsx")
        self.assertEqual(ultima, "RESULTADO: IGUAL")


# =====================================================================
# Pruebas de formato
# =====================================================================
class PruebasFormato(Base):
    def test_cr_solo(self):
        _, ultima = self.texto("a\rb\rc\r", "a\nb\nc\n")
        self.assertIn("IGUAL SALVO FORMATO", ultima)
        self.assertIn("proyecto CR, local LF", ultima)

    def test_saltos_mixtos(self):
        _, ultima = self.texto("a\r\nb\nc\r\n", "a\nb\nc\n")
        self.assertIn("proyecto mixto (CRLF 2, LF 1), local LF", ultima)

    def test_bom(self):
        _, ultima = self.bytes_(b"\xef\xbb\xbfhola\n", b"hola\n")
        self.assertIn("IGUAL SALVO FORMATO (BOM al inicio: proyecto sí, local no)", ultima)

    def test_espacios_y_tabs_al_final(self):
        _, ultima = self.texto("a  \nb\t\nc\n", "a\nb\nc\n")
        self.assertIn("espacios o tabs al final de línea: en 2 líneas", ultima)

    def test_salto_final(self):
        _, ultima = self.texto("a\nb", "a\nb\n")
        self.assertIn("salto de línea al final del archivo: proyecto no, local sí", ultima)

    def test_varias_diferencias_de_formato_juntas(self):
        a = "Canción \r\nsegunda\r\n"
        b = unicodedata.normalize("NFD", "Canción\nsegunda")
        _, ultima = self.texto(a, b)
        for esperado in ("saltos de línea", "espacios o tabs", "salto de línea al final", "Unicode"):
            self.assertIn(esperado, ultima)

    def test_dos_lineas_vacias_al_final_no_son_formato(self):
        # Solo UN salto final es formato; líneas vacías de más son contenido.
        salida, ultima = self.texto("a\n\n", "a\n")
        self.assertIn("NO IGUAL", ultima)
        self.assertIn("SOLO EN PROYECTO, línea 2: (línea vacía)", salida)

    def test_utf16_contra_utf8(self):
        _, ultima = self.bytes_("hola\nmundo\n".encode("utf-16"), "hola\nmundo\n".encode("utf-8"))
        self.assertIn("IGUAL SALVO FORMATO", ultima)
        self.assertIn("codificación: proyecto UTF-16", ultima)

    def test_formato_y_contenido_juntos(self):
        # Con CRLF, el único cambio real está en la línea 3 y es lo único que se cuenta.
        salida, ultima = self.texto("l1\r\nl2  \r\nl3 texto viejo aqui\r\nl4\r\n", "l1\nl2\nl3 texto nuevo aqui\nl4\n")
        self.assertIn("Formato: además hay diferencias de formato", salida)
        self.assertIn("línea 3", salida)
        self.assertIn("[[viejo]]", salida)
        self.assertEqual(ultima, "RESULTADO: NO IGUAL (1 línea distinta, 0 solo en proyecto, 0 solo en local)")

    def test_la_numeracion_no_corta_en_separadores_raros(self):
        #  , \x0b, \x0c y \x85 NO son saltos de línea para un editor común.
        raro = "uno dos\x0btres\x0ccuatro\x85cinco"
        salida, _ = self.texto(raro + "\nlinea dos vieja\n", raro + "\nlinea dos nueva\n")
        self.assertIn("línea 2", salida)

    def test_mismo_contenido_distinta_fecha(self):
        rp = self.archivo("a.md", b"hola\n", "proyecto")
        rl = self.archivo("a.md", b"hola\n", "local")
        os.utime(rl, (1000000, 1000000))
        _, ultima = self.correr(rp, rl)
        self.assertEqual(ultima, "RESULTADO: IGUAL")


# =====================================================================
# Pruebas de contenido
# =====================================================================
class PruebasContenido(Base):
    def test_bytes_invalidos_distintos_no_parecen_iguales(self):
        # Con errors="replace" ambos serían U+FFFD y el programa diría "igual".
        salida, ultima = self.bytes_(b"hola \xff mundo\n", b"hola \xfe mundo\n")
        self.assertTrue(ultima.startswith("RESULTADO: NO IGUAL"))
        self.assertIn("[[<0xFF>", salida)
        self.assertIn("[[<0xFE>", salida)
        self.assertIn("no son UTF-8 válido", salida)

    def test_bytes_invalidos_iguales(self):
        _, ultima = self.bytes_(b"hola \xff mundo\n", b"hola \xff mundo\n")
        self.assertEqual(ultima, "RESULTADO: IGUAL")

    def test_espacio_no_separable_se_ve(self):
        salida, ultima = self.texto("uno dos\n", "uno dos\n")
        self.assertIn("NO IGUAL", ultima)
        self.assertIn("<NBSP>", salida)
        self.assertNotIn(" ", salida)

    def test_tab_dentro_de_la_linea_se_ve(self):
        salida, _ = self.texto("x y z\n", "x\ty z\n")
        self.assertIn("<TAB>", salida)

    def test_caracteres_de_control_no_salen_crudos(self):
        salida, _ = self.texto("rojo \x1b[31m fin\n", "rojo \x1b[32m fin\n")
        self.assertNotIn("\x1b", salida)
        self.assertIn("<ESC>", salida)

    def test_archivos_vacios(self):
        salida, ultima = self.bytes_(b"", b"")
        self.assertEqual(ultima, "RESULTADO: IGUAL")
        self.assertIn("0 bytes, 0 líneas", salida)
        salida, ultima = self.bytes_(b"", b"hola\n")
        self.assertEqual(ultima, "RESULTADO: NO IGUAL (0 líneas distintas, 0 solo en proyecto, 1 solo en local)")
        self.assertIn("SOLO EN LOCAL, línea 1: hola", salida)

    def test_linea_gigante(self):
        linea = " ".join("p%d" % i for i in range(40000))                  # ~240.000 caracteres
        nueva = linea.replace("p20000", "CAMBIO", 1)
        t0 = time.perf_counter()
        salida, ultima = self.texto(linea + "\n", nueva + "\n")
        self.assertLess(time.perf_counter() - t0, 10)
        self.assertIn("[[p20000]]", salida)
        self.assertLess(len(salida), 1500)
        self.assertIn("NO IGUAL (1 línea", ultima)

    def test_archivo_grande_con_un_cambio(self):
        lineas = lineas_unicas(100000, semilla=7)                          # ~ 6 MB
        nuevas = list(lineas)
        nuevas[54321] = nuevas[54321].replace("w", "X", 1)
        t0 = time.perf_counter()
        salida, ultima = self.texto("\n".join(lineas) + "\n", "\n".join(nuevas) + "\n")
        self.assertLess(time.perf_counter() - t0, 20)
        self.assertIn("línea 54322", salida)
        self.assertIn("NO IGUAL (1 línea distinta", ultima)

    def test_mas_diferencias_que_el_maximo(self):
        base = lineas_unicas(10, semilla=3)
        nuevas = [l.replace(l.split()[0], "CAMBIADA%d" % n) for n, l in enumerate(base)]
        with mock.patch.object(comparador, "MAX_DIFERENCIAS_MOSTRADAS", 3):
            salida, ultima = self.texto("\n".join(base) + "\n", "\n".join(nuevas) + "\n")
        self.assertIn("Diferencia 3 de 10", salida)
        self.assertNotIn("Diferencia 4 de 10", salida)
        self.assertIn("Faltan 7 diferencias por mostrar", salida)
        self.assertIn("MAX_DIFERENCIAS_MOSTRADAS", salida)
        self.assertEqual(ultima, "RESULTADO: NO IGUAL (10 líneas distintas, 0 solo en proyecto, 0 solo en local)")

    def test_linea_sola_se_recorta_y_avisa(self):
        larga = "palabra " * 100
        salida, _ = self.texto("a\n", "a\n" + larga + "\n")
        self.assertIn("...(+", salida)
        self.assertIn("caracteres)", salida)
        with mock.patch.object(comparador, "MAX_CARACTERES_LINEA_SOLA", 20):
            salida, _ = self.texto("a\n", "a\n" + larga + "\n")
        self.assertIn("SOLO EN LOCAL, línea 2: palabra palabra pala ...(+%d caracteres)" % (len(larga.rstrip()) - 20), salida)

    def test_parrafo_movido(self):
        p1, p2, p3 = lineas_unicas(3, semilla=11)
        salida, ultima = self.texto("\n".join([p1, p2, p3]) + "\n", "\n".join([p2, p3, p1]) + "\n")
        self.assertIn("MOVIDA de lugar: línea 1 en proyecto -> línea 3 en local", salida)
        self.assertEqual(ultima, "RESULTADO: NO IGUAL (0 líneas distintas, 0 solo en proyecto, 0 solo en local, 1 línea movida)")

    def test_parrafo_reescrito_no_se_muestra_como_cambio(self):
        viejo = " ".join("viejo%d" % i for i in range(80))
        nuevo = " ".join("otro%d" % i for i in range(80))
        salida, ultima = self.texto("inicio\n" + viejo + "\nfin\n", "inicio\n" + nuevo + "\nfin\n")
        self.assertIn("0 líneas distintas, 1 solo en proyecto, 1 solo en local", ultima)
        self.assertNotIn("[[", salida)

    def test_muchas_ediciones_dispersas_en_archivo_grande(self):
        # Antes de usar anclas (método de paciencia) esto tardaba minutos.
        lineas = lineas_unicas(60000, semilla=21)
        cambiar, borrar = set(range(0, 60000, 200)), set(range(100, 60000, 200))
        nuevas = [l + " extra" if i in cambiar else l for i, l in enumerate(lineas) if i not in borrar]
        t0 = time.perf_counter()
        _, ultima = self.texto("\n".join(lineas) + "\n", "\n".join(nuevas) + "\n")
        self.assertLess(time.perf_counter() - t0, 15)
        self.assertEqual(ultima, "RESULTADO: NO IGUAL (%d líneas distintas, %d solo en proyecto, 0 solo en local)"
                         % (len(cambiar), len(borrar)))

    def test_zona_repetitiva_enorme_avisa_que_es_aproximada(self):
        rep = ["---", "texto fijo de relleno"] * 100
        with mock.patch.object(comparador, "MAX_COMPARACIONES_ZONA", 100):
            salida, ultima = self.texto("\n".join(rep) + "\n", "\n".join(rep[1:] + ["---"]) + "\n")
        self.assertIn("AVISO: hay una zona muy grande y repetitiva", salida)
        self.assertIn("NO IGUAL", ultima)                                  # nunca dice que son iguales

    def test_alinear_cubre_todo_y_lo_igual_es_igual(self):
        azar = random.Random(5)
        for _ in range(2000):
            simbolos = azar.choice([2, 3, 6, 30, 1000])                    # pocos símbolos = muchas repetidas
            a = [str(azar.randrange(simbolos)) for _ in range(azar.randrange(40))]
            b = list(a)
            for _ in range(azar.randrange(6)):
                b.insert(azar.randrange(len(b) + 1), str(azar.randrange(simbolos)))
                if b and azar.random() < 0.5:
                    del b[azar.randrange(len(b))]
            ops, _ = comparador.alinear(a, b)
            i = j = 0
            for tipo, i1, i2, j1, j2 in ops:
                self.assertEqual((i1, j1), (i, j))                         # sin huecos ni solapes
                if tipo == "equal":
                    self.assertEqual(a[i1:i2], b[j1:j2])                   # lo 'equal' es igual de verdad
                i, j = i2, j2
            self.assertEqual((i, j), (len(a), len(b)))

    def test_zonas_marcadas_explican_toda_la_diferencia(self):
        # Lo que queda FUERA de las zonas marcadas tiene que ser idéntico en las dos líneas:
        # si no, el programa estaría escondiendo una diferencia.
        azar = random.Random(7)
        simbolos = ["a", "b", "ab", " ", "  ", "\t", "é", "e\u0301", ".", ",", "_", "1", "22",
                    "\u00a0", "\x1b", "\udcff", "palabra", "x-y"]
        for contexto in (0, 3, 40):
            with mock.patch.object(comparador, "CONTEXTO_CARACTERES", contexto):
                for _ in range(2500):
                    x = "".join(azar.choice(simbolos) for _ in range(azar.randrange(30)))
                    y = list(x)
                    for _ in range(azar.randrange(1, 4)):
                        if y and azar.random() < 0.5:
                            del y[azar.randrange(len(y))]
                        y.insert(azar.randrange(len(y) + 1), azar.choice(simbolos))
                    y = "".join(y)
                    if x == y:
                        continue
                    zonas = [z for g in comparador.grupos_de_cambios(x, y) for z in g]
                    self.assertTrue(zonas, (x, y))
                    px = py = 0
                    for sx, ex, sy, ey in zonas:
                        self.assertEqual(x[px:sx], y[py:sy], (x, y))
                        px, py = ex, ey
                    self.assertEqual(x[px:], y[py:], (x, y))

    def test_linea_agregada_al_lado_de_una_editada(self):
        l1, l2, l3, l4 = lineas_unicas(4, semilla=5)
        editada = l2.replace(l2.split()[2], "CAMBIADA")
        nueva = "Una línea nueva sin parecido alguno con las demás del archivo."
        salida, ultima = self.texto("\n".join([l1, l2, l3, l4]) + "\n", "\n".join([l1, nueva, editada, l3, l4]) + "\n")
        self.assertEqual(ultima, "RESULTADO: NO IGUAL (1 línea distinta, 0 solo en proyecto, 1 solo en local)")
        self.assertIn("Proyecto,  línea 2", salida)
        self.assertIn("Local,     línea 3", salida)
        self.assertIn("SOLO EN LOCAL, línea 2: " + nueva, salida)

    def test_varios_cambios_lejanos_en_una_linea(self):
        base = [("palabra%03d" % i) for i in range(200)]
        nueva = list(base)
        nueva[20], nueva[100], nueva[180] = "AAA", "BBB", "CCC"
        salida, _ = self.texto(" ".join(base) + "\n", " ".join(nueva) + "\n")
        self.assertEqual(salida.count("  Proyecto,"), 3)
        for marca in ("[[palabra020]]", "[[palabra100]]", "[[palabra180]]", "[[AAA]]", "[[BBB]]", "[[CCC]]"):
            self.assertIn(marca, salida)

    def test_cambios_cercanos_se_muestran_juntos(self):
        base = [("palabra%03d" % i) for i in range(200)]
        nueva = list(base)
        nueva[100], nueva[102] = "AAA", "BBB"
        salida, _ = self.texto(" ".join(base) + "\n", " ".join(nueva) + "\n")
        self.assertEqual(salida.count("  Proyecto,"), 1)
        self.assertIn("[[palabra100]] palabra101 [[palabra102]]", salida)

    def test_muchos_cambios_en_una_linea_se_resumen(self):
        base = [("palabra%03d" % i) for i in range(400)]
        nueva = list(base)
        for i in range(10, 400, 60):
            nueva[i] = "X%d" % i
        salida, _ = self.texto(" ".join(base) + "\n", " ".join(nueva) + "\n")
        self.assertEqual(salida.count("  Proyecto,"), 3)
        self.assertIn("(+4 cambios más en esta línea)", salida)

    def test_cambio_al_principio_y_al_final_de_la_linea(self):
        salida, _ = self.texto("viejo resto de la linea fin\n", "nuevo resto de la linea fin\n")
        self.assertIn("Proyecto,  línea 1, col. 1: [[viejo]] resto", salida)        # sin "..." al principio
        salida, _ = self.texto("inicio de la linea viejo\n", "inicio de la linea nuevo\n")
        linea = [l for l in salida.split("\n") if l.startswith("  Proyecto,")][0]
        self.assertTrue(linea.endswith("col. 20: inicio de la linea [[viejo]]"))   # sin "..." al final


# =====================================================================
# Rutas, nombres y errores
# =====================================================================
class PruebasRutas(Base):
    def test_las_dos_rutas_son_el_mismo_archivo(self):
        ruta = self.archivo("a.md", b"hola\n")
        _, ultima = self.correr(ruta, ruta)
        self.assertTrue(ultima.startswith("RESULTADO: ERROR (las dos rutas apuntan al mismo archivo"))
        enlace = os.path.join(self.dir, "enlace.md")
        os.symlink(ruta, enlace)
        _, ultima = self.correr(ruta, enlace)
        self.assertIn("mismo archivo", ultima)
        duro = os.path.join(self.dir, "duro.md")
        os.link(ruta, duro)
        _, ultima = self.correr(ruta, duro)
        self.assertIn("mismo archivo", ultima)

    def test_ruta_que_es_carpeta(self):
        ruta = self.archivo("a.md", b"hola\n")
        for pares in ((self.dir, ruta), (ruta, self.dir)):
            salida, ultima = self.correr(*pares)
            self.assertTrue(ultima.startswith("RESULTADO: ERROR (la ruta de "))
            self.assertIn("es una carpeta", ultima)

    def test_rutas_con_espacios_comillas_y_ñ(self):
        ruta = self.archivo("Diseño final.md", b"hola\n", "Mis Documentos")
        _, ultima = self.correr(ruta, '  "%s"  ' % ruta)
        self.assertEqual(ultima, "RESULTADO: ERROR (las dos rutas apuntan al mismo archivo: no hay nada que comparar)")
        otra = self.archivo("Diseño final.md", b"hola\n", "Otra Carpeta")
        _, ultima = self.correr("'%s'" % ruta, otra)
        self.assertEqual(ultima, "RESULTADO: IGUAL")

    def test_nombre_con_otra_forma_unicode_en_el_disco(self):
        nfc = "Diseño.md"
        nfd = unicodedata.normalize("NFD", nfc)
        self.archivo(nfd, b"hola\n", "proyecto")                           # en el disco está en NFD
        local = self.archivo(nfc, b"hola\n", "local")
        salida, ultima = self.correr(os.path.join(self.dir, "proyecto", nfc), local)   # pido NFC
        self.assertIn("AVISO: no encontré", salida)
        self.assertTrue(ultima.startswith("RESULTADO: IGUAL | los nombres solo difieren en la forma Unicode"))

    def test_nombres_nfc_contra_nfd(self):
        nfc = "Diseño.md"
        _, ultima = self.texto("a\n", "a\n", nombre_p=nfc, nombre_l=unicodedata.normalize("NFD", nfc))
        self.assertIn("los nombres solo difieren en la forma Unicode", ultima)

    def test_extension_en_mayusculas_y_bytes_nulos_son_binarios(self):
        _, ultima = self.bytes_(b"abc", b"abd", "x.PDF", "x.PDF")
        self.assertIn("binarios", ultima)
        _, ultima = self.bytes_(b"abc\x00def", b"abc\x00deg", "x.txt", "x.txt")
        self.assertIn("binarios", ultima)
        _, ultima = self.bytes_(b"%PDF-1.7 texto", b"%PDF-1.7 otro", "renombrado.txt", "renombrado.txt")
        self.assertIn("binarios", ultima)

    def test_un_binario_y_un_texto(self):
        _, ultima = self.bytes_(b"hola\n", b"\x89PNG\r\n\x1a\n\x00\x00", "a.md", "a.md")
        self.assertIn("NO IGUAL (binarios", ultima)

    def test_archivo_grande_solo_hash(self):
        with mock.patch.object(comparador, "TAMANO_MAXIMO_TEXTO_MB", 0.001):          # ~1 KB
            salida, ultima = self.texto("a" * 5000 + "\n", "a" * 4999 + "b\n")
            self.assertIn("NO IGUAL (archivos grandes: solo se comparó el hash)", ultima)
            self.assertIn("supera los 0.001 MB", salida)
            _, ultima = self.texto("a" * 5000 + "\n", "a" * 5000 + "\n")
            self.assertEqual(ultima, "RESULTADO: IGUAL")

    def test_error_de_lectura_no_se_disfraza_de_solo_local(self):
        ruta_p, ruta_l = self.archivo("a.md", b"x\n", "p"), self.archivo("a.md", b"y\n", "l")
        with mock.patch.object(comparador, "leer", side_effect=PermissionError(13, "Permission denied")):
            _, ultima = self.correr(ruta_p, ruta_l)
        self.assertEqual(ultima, "RESULTADO: ERROR (no se pudo leer un archivo: Permission denied)")

    def test_main_siempre_termina_en_resultado(self):
        buf = io.StringIO()
        with mock.patch.object(comparador, "comparar", side_effect=RuntimeError("boom")):
            with contextlib.redirect_stdout(buf):
                comparador.main()
        self.assertEqual(buf.getvalue().strip(), "RESULTADO: ERROR (RuntimeError: boom)")

    def test_salida_con_consola_que_no_soporta_todos_los_caracteres(self):
        ruta_p = self.archivo("a.md", "flecha → fin\n".encode("utf-8"), "p")
        ruta_l = self.archivo("a.md", "flecha ← fin\n".encode("utf-8"), "l")
        crudo = io.BytesIO()
        consola = io.TextIOWrapper(crudo, encoding="cp1252", errors="strict", write_through=True)
        with mock.patch.multiple(comparador, RUTA_PROYECTO=ruta_p, RUTA_LOCAL=ruta_l):
            with mock.patch.object(sys, "stdout", consola):
                comparador.main()
        self.assertIn(b"RESULTADO: NO IGUAL", crudo.getvalue())


# =====================================================================
# Solo lectura
# =====================================================================
class PruebasSoloLectura(Base):
    def foto(self):
        """Nombre, tamaño, fecha y hash de todo lo que hay en la carpeta temporal."""
        res = {}
        for raiz, carpetas, archivos in os.walk(self.dir):
            for nombre in carpetas + archivos:
                ruta = os.path.join(raiz, nombre)
                datos = None
                if os.path.isfile(ruta):
                    with open(ruta, "rb") as f:
                        datos = hashlib.sha256(f.read()).hexdigest()
                st = os.stat(ruta)
                res[ruta] = (st.st_size, st.st_mtime_ns, datos)
        return res

    def test_nunca_escribe_ni_modifica(self):
        global _ESPIANDO
        casos = [
            (b"hola\nmundo\n", b"hola\nmundo\n", "a.md"),
            (b"hola\nmundo\n", b"hola\r\nmundo\r\n", "a.md"),
            (b"hola\nmundo\n", b"hola\nplaneta\n", "a.md"),
            (b"%PDF-1 a", b"%PDF-1 b", "a.pdf"),
            (b"\xef\xbb\xbfx\n", b"x\n", "a.md"),
        ]
        rutas = []
        for n, (bp, bl, nombre) in enumerate(casos):
            rutas.append((self.archivo(nombre, bp, "p%d" % n), self.archivo(nombre, bl, "l%d" % n)))
        rutas.append((os.path.join(self.dir, "falta.md"), rutas[0][0]))
        antes = self.foto()
        del _ESCRITURAS[:]
        _ESPIANDO = True
        try:
            for rp, rl in rutas:
                self.correr(rp, rl)
        finally:
            _ESPIANDO = False
        self.assertEqual(_ESCRITURAS, [])
        self.assertEqual(self.foto(), antes)


if __name__ == "__main__":
    unittest.main()
