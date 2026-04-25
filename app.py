from flask import Flask, request, jsonify, send_from_directory, send_file
from flask_cors import CORS
from datetime import datetime
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
import os
import tempfile
from pathlib import Path
import psycopg2
import psycopg2.extras

app = Flask(__name__, static_folder='../frontend', static_url_path='')
CORS(app)

DATABASE_URL = os.environ.get('DATABASE_URL')
FRONTEND_DIR = Path(__file__).resolve().parent.parent / 'frontend'

MESES = ["Janeiro","Fevereiro","Março","Abril","Maio","Junho",
         "Julho","Agosto","Setembro","Outubro","Novembro","Dezembro"]

# ── Banco de dados ────────────────────────────────────────────────────────────

def get_conn():
    """Retorna conexão com o PostgreSQL."""
    return psycopg2.connect(DATABASE_URL)

def init_db():
    """Cria as tabelas se não existirem."""
    conn = get_conn()
    cur  = conn.cursor()
    lock_id = 734512901

    try:
        # Evita corrida entre workers ao criar schema no startup.
        cur.execute("SELECT pg_advisory_lock(%s)", (lock_id,))

        cur.execute("""
            CREATE TABLE IF NOT EXISTS lancamentos (
                id         SERIAL PRIMARY KEY,
                mes        VARCHAR(20),
                data       VARCHAR(20),
                descricao  TEXT NOT NULL,
                categoria  VARCHAR(50),
                tipo       VARCHAR(10) NOT NULL,
                valor      NUMERIC(12,2) NOT NULL,
                pagamento  VARCHAR(50),
                obs        TEXT,
                criado_em  TIMESTAMP DEFAULT NOW()
            );
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS fixos (
                id               SERIAL PRIMARY KEY,
                tipo             VARCHAR(10) NOT NULL,
                descricao        TEXT NOT NULL,
                valor            NUMERIC(12,2) NOT NULL,
                categoria        VARCHAR(50),
                pagamento        VARCHAR(50),
                parcelado        BOOLEAN DEFAULT FALSE,
                parcela_atual    INTEGER DEFAULT 1,
                parcela_total    INTEGER DEFAULT 1,
                data_inicio      VARCHAR(10),
                ativo            BOOLEAN DEFAULT TRUE,
                criado_em        TIMESTAMP DEFAULT NOW()
            );
        """)

        # Migra schema antigo de fixos sem exigir recriacao de tabela.
        cur.execute("ALTER TABLE fixos ADD COLUMN IF NOT EXISTS parcelado BOOLEAN DEFAULT FALSE")
        cur.execute("ALTER TABLE fixos ADD COLUMN IF NOT EXISTS parcela_atual INTEGER DEFAULT 1")
        cur.execute("ALTER TABLE fixos ADD COLUMN IF NOT EXISTS parcela_total INTEGER DEFAULT 1")
        cur.execute("ALTER TABLE fixos ADD COLUMN IF NOT EXISTS data_inicio VARCHAR(10)")
        cur.execute("ALTER TABLE fixos ADD COLUMN IF NOT EXISTS ativo BOOLEAN DEFAULT TRUE")
        cur.execute("ALTER TABLE fixos ADD COLUMN IF NOT EXISTS criado_em TIMESTAMP DEFAULT NOW()")

        cur.execute("""
            CREATE TABLE IF NOT EXISTS lancamentos_gerados (
                id               SERIAL PRIMARY KEY,
                fixo_id          INTEGER REFERENCES fixos(id) ON DELETE CASCADE,
                mes              VARCHAR(20),
                ano              INTEGER,
                valor            NUMERIC(12,2),
                gerado_em        TIMESTAMP DEFAULT NOW()
            );
        """)

        # Insere fixos padrão se tabela estiver vazia
        cur.execute("SELECT COUNT(*) FROM fixos;")
        if cur.fetchone()[0] == 0:
            fixos_padrao = [
                ('ENTRADA', 'Salário',          5000.00, 'Salário',           ''),
                ('ENTRADA', 'Aluguel Imóvel',   1200.00, 'Outros (Entrada)',   ''),
                ('ENTRADA', 'CDB/Dividendos',    300.00, 'Investimentos',      ''),
                ('SAÍDA',   'Aluguel Apt.',     1500.00, 'Moradia',            'Débito Automático'),
                ('SAÍDA',   'Plano de Saúde',    280.00, 'Saúde',              'Débito Automático'),
                ('SAÍDA',   'Internet',          100.00, 'Contas/Serviços',    'Débito Automático'),
                ('SAÍDA',   'Academia',           90.00, 'Saúde',              'Débito Automático'),
                ('SAÍDA',   'Netflix',            45.00, 'Lazer',              'Cartão Crédito'),
                ('SAÍDA',   'Spotify',            21.00, 'Lazer',              'Cartão Crédito'),
            ]
            cur.executemany("""
                INSERT INTO fixos (tipo, descricao, valor, categoria, pagamento)
                VALUES (%s, %s, %s, %s, %s)
            """, fixos_padrao)

        conn.commit()
    finally:
        try:
            cur.execute("SELECT pg_advisory_unlock(%s)", (lock_id,))
            conn.commit()
        except Exception:
            conn.rollback()
        cur.close()
        conn.close()


def bootstrap_database():
    """Inicializa o banco no startup (incluindo deploy com Gunicorn)."""
    if not DATABASE_URL:
        print("  ⚠️  DATABASE_URL não definida!")
        return

    try:
        init_db()
        print("  ✅ Tabelas verificadas/criadas")
    except Exception as e:
        print(f"  ❌ Erro ao iniciar banco: {e}")


# Garante init do banco também quando o app roda via Gunicorn.
bootstrap_database()

# ── Helpers ───────────────────────────────────────────────────────────────────

def mes_da_data(data_str):
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d/%m/%y"):
        try:
            dt = datetime.strptime(data_str.strip(), fmt)
            return MESES[dt.month - 1]
        except ValueError:
            continue
    return ""

def verificar_e_gerar_parcelas(mes, ano):
    """Verifica fixos parcelados e gera lançamentos se necessário."""
    conn = get_conn()
    cur  = conn.cursor()
    
    # Busca todos os fixos parcelados ativos
    cur.execute("""
        SELECT id, valor, parcela_atual, parcela_total, data_inicio, tipo
        FROM fixos WHERE parcelado=TRUE AND ativo=TRUE
    """)
    fixos_parcelados = cur.fetchall()
    
    for fixo_id, valor, parcela_atual, parcela_total, data_inicio, tipo in fixos_parcelados:
        if not data_inicio:
            continue
        
        # Verifica se já foi gerado para este mês
        cur.execute("""
            SELECT id FROM lancamentos_gerados 
            WHERE fixo_id=%s AND mes=%s AND ano=%s
        """, (fixo_id, mes, ano))
        
        if cur.fetchone():
            # Já foi gerado, pula
            continue
        
        # Calcula se este mês está no range de parcelas
        # data_inicio é "01/05/2026" (1º dia do mês inicial)
        try:
            dt_inicio = datetime.strptime(data_inicio, "%d/%m/%Y")
            mes_inicio = dt_inicio.month
            ano_inicio = dt_inicio.year
            
            # Calcula quantos meses passaram desde o início
            meses_decorridos = (ano - ano_inicio) * 12 + (MESES.index(mes) - (mes_inicio - 1))
            
            # Se dentro do range de parcelas
            if 0 <= meses_decorridos < parcela_total:
                # Gera lançamento
                cur.execute("""
                    INSERT INTO lancamentos_gerados (fixo_id, mes, ano, valor)
                    VALUES (%s, %s, %s, %s)
                """, (fixo_id, mes, ano, valor))
                
                # Atualiza parcela_atual
                nova_parcela = meses_decorridos + 1
                cur.execute("""
                    UPDATE fixos SET parcela_atual=%s WHERE id=%s
                """, (nova_parcela, fixo_id))
                
                # Se chegou no final, desativa
                if nova_parcela >= parcela_total:
                    cur.execute("UPDATE fixos SET ativo=FALSE WHERE id=%s", (fixo_id,))
        except ValueError:
            continue
    
    conn.commit(); cur.close(); conn.close()

def totais_fixos():
    """Retorna total de rendas e gastos fixos."""
    conn = get_conn()
    cur  = conn.cursor()
    cur.execute("SELECT tipo, SUM(valor) FROM fixos WHERE ativo=TRUE GROUP BY tipo")
    rows = cur.fetchall()
    cur.close(); conn.close()
    resultado = {'ENTRADA': 0.0, 'SAÍDA': 0.0}
    for tipo, total in rows:
        resultado[tipo] = float(total or 0)
    return resultado

# ── Rotas API ─────────────────────────────────────────────────────────────────

@app.route('/api/health', methods=['GET'])
def healthcheck():
    """Healthcheck simples para validar API e conexão com banco."""
    db_ok = False
    db_error = None

    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute('SELECT 1')
        cur.fetchone()
        cur.close()
        conn.close()
        db_ok = True
    except Exception as e:
        db_error = str(e)

    status = 200 if db_ok else 503
    return jsonify({
        'api': 'ok',
        'database': 'ok' if db_ok else 'error',
        'database_error': db_error
    }), status

@app.route('/api/adicionar-lancamento', methods=['POST'])
def adicionar_lancamento():
    try:
        dados = request.json or {}
        for campo in ['data', 'descricao', 'categoria', 'tipo', 'valor']:
            if not str(dados.get(campo, '')).strip():
                return jsonify({"erro": f"Campo obrigatório: {campo}"}), 400

        mes  = mes_da_data(str(dados['data']))
        tipo = dados['tipo'].upper()

        conn = get_conn()
        cur  = conn.cursor()
        cur.execute("""
            INSERT INTO lancamentos (mes, data, descricao, categoria, tipo, valor, pagamento, obs)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (
            mes, dados['data'], dados['descricao'], dados['categoria'],
            tipo, float(dados['valor']),
            dados.get('pagamento', ''), dados.get('obs', '')
        ))
        novo_id = cur.fetchone()[0]
        conn.commit(); cur.close(); conn.close()

        return jsonify({"sucesso": True, "mensagem": "Lançamento adicionado!", "id": novo_id, "mes": mes}), 201

    except Exception as e:
        return jsonify({"erro": str(e)}), 500


@app.route('/api/lancamentos', methods=['GET'])
def listar_lancamentos():
    try:
        mes_filtro = request.args.get('mes', '').strip()
        conn = get_conn()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        if mes_filtro:
            cur.execute("""
                SELECT * FROM lancamentos WHERE LOWER(mes)=LOWER(%s)
                ORDER BY criado_em DESC
            """, (mes_filtro,))
        else:
            cur.execute("SELECT * FROM lancamentos ORDER BY criado_em DESC")

        rows = cur.fetchall()
        cur.close(); conn.close()

        lancamentos = []
        for r in rows:
            lancamentos.append({
                "id":        r['id'],
                "mes":       r['mes'],
                "data":      r['data'],
                "descricao": r['descricao'],
                "categoria": r['categoria'],
                "tipo":      r['tipo'],
                "valor":     float(r['valor']),
                "pagamento": r['pagamento'],
                "obs":       r['obs'],
            })

        return jsonify({"lancamentos": lancamentos, "total": len(lancamentos)}), 200

    except Exception as e:
        return jsonify({"erro": str(e)}), 500


@app.route('/api/lancamentos/<int:lancamento_id>', methods=['PUT'])
def editar_lancamento(lancamento_id):
    try:
        dados = request.json or {}

        conn = get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM lancamentos WHERE id=%s", (lancamento_id,))
        atual = cur.fetchone()

        if not atual:
            cur.close(); conn.close()
            return jsonify({"erro": "Lançamento não encontrado"}), 404

        data = str(dados.get('data', atual['data'])).strip()
        descricao = str(dados.get('descricao', atual['descricao'])).strip()
        categoria = str(dados.get('categoria', atual['categoria'] or '')).strip()
        tipo = str(dados.get('tipo', atual['tipo'])).upper().strip()
        pagamento = str(dados.get('pagamento', atual['pagamento'] or '')).strip()
        obs = str(dados.get('obs', atual['obs'] or '')).strip()

        valor_recebido = dados.get('valor', atual['valor'])
        try:
            valor = float(valor_recebido)
        except (TypeError, ValueError):
            cur.close(); conn.close()
            return jsonify({"erro": "Valor inválido"}), 400

        if not data or not descricao or not categoria or tipo not in ('ENTRADA', 'SAÍDA') or valor <= 0:
            cur.close(); conn.close()
            return jsonify({"erro": "Dados inválidos para atualização"}), 400

        mes = mes_da_data(data)
        if not mes:
            cur.close(); conn.close()
            return jsonify({"erro": "Data inválida"}), 400

        cur2 = conn.cursor()
        cur2.execute("""
            UPDATE lancamentos
            SET mes=%s, data=%s, descricao=%s, categoria=%s, tipo=%s, valor=%s, pagamento=%s, obs=%s
            WHERE id=%s
        """, (mes, data, descricao, categoria, tipo, valor, pagamento, obs, lancamento_id))

        conn.commit()
        cur2.close(); cur.close(); conn.close()

        return jsonify({"sucesso": True, "mes": mes}), 200

    except Exception as e:
        return jsonify({"erro": str(e)}), 500


@app.route('/api/lancamentos/<int:lancamento_id>', methods=['DELETE'])
def deletar_lancamento(lancamento_id):
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("DELETE FROM lancamentos WHERE id=%s", (lancamento_id,))

        if cur.rowcount == 0:
            conn.rollback(); cur.close(); conn.close()
            return jsonify({"erro": "Lançamento não encontrado"}), 404

        conn.commit()
        cur.close(); conn.close()
        return jsonify({"sucesso": True}), 200

    except Exception as e:
        return jsonify({"erro": str(e)}), 500


@app.route('/api/resumo', methods=['GET'])
def resumo():
    try:
        mes_filtro = request.args.get('mes', '').strip()
        ano_filtro = request.args.get('ano', str(datetime.now().year)).strip()
        
        conn = get_conn()
        cur  = conn.cursor()

        # Gera parcelas se necessário
        if mes_filtro:
            ano = int(ano_filtro) if ano_filtro else datetime.now().year
            verificar_e_gerar_parcelas(mes_filtro, ano)

        if mes_filtro:
            cur.execute("""
                SELECT tipo, SUM(valor) FROM lancamentos
                WHERE LOWER(mes)=LOWER(%s) GROUP BY tipo
            """, (mes_filtro,))
        else:
            cur.execute("SELECT tipo, SUM(valor) FROM lancamentos GROUP BY tipo")

        rows = cur.fetchall()
        
        entradas = saidas = 0.0
        for tipo, total in rows:
            if tipo == 'ENTRADA': entradas = float(total or 0)
            elif tipo == 'SAÍDA': saidas   = float(total or 0)

        # Adiciona lançamentos gerados de fixos parcelados
        if mes_filtro:
            cur.execute("""
                SELECT SUM(CASE WHEN f.tipo='ENTRADA' THEN lg.valor ELSE 0 END),
                       SUM(CASE WHEN f.tipo='SAÍDA' THEN lg.valor ELSE 0 END)
                FROM lancamentos_gerados lg
                JOIN fixos f ON lg.fixo_id = f.id
                WHERE lg.mes=%s
            """, (mes_filtro,))
            entrada_parc, saida_parc = cur.fetchone()
            if entrada_parc: entradas += float(entrada_parc)
            if saida_parc: saidas += float(saida_parc)

        fixos = totais_fixos()
        
        cur.close(); conn.close()

        return jsonify({
            "entradas":     round(entradas, 2),
            "saidas":       round(saidas,   2),
            "saldo":        round(entradas - saidas, 2),
            "renda_fixa":   round(fixos['ENTRADA'], 2),
            "gastos_fixos": round(fixos['SAÍDA'],   2),
        }), 200

    except Exception as e:
        return jsonify({"erro": str(e)}), 500


@app.route('/api/categorias-resumo', methods=['GET'])
def categorias_resumo():
    try:
        mes_filtro = request.args.get('mes', '').strip()
        conn = get_conn()
        cur  = conn.cursor()

        if mes_filtro:
            cur.execute("""
                SELECT categoria, SUM(valor) FROM lancamentos
                WHERE tipo='SAÍDA' AND LOWER(mes)=LOWER(%s)
                GROUP BY categoria ORDER BY SUM(valor) DESC
            """, (mes_filtro,))
        else:
            cur.execute("""
                SELECT categoria, SUM(valor) FROM lancamentos
                WHERE tipo='SAÍDA'
                GROUP BY categoria ORDER BY SUM(valor) DESC
            """)

        rows = cur.fetchall()
        cur.close(); conn.close()

        return jsonify({"categorias": [
            {"categoria": r[0], "total": float(r[1])} for r in rows
        ]}), 200

    except Exception as e:
        return jsonify({"erro": str(e)}), 500


@app.route('/api/meses-disponiveis', methods=['GET'])
def meses_disponiveis():
    try:
        conn = get_conn()
        cur  = conn.cursor()
        cur.execute("SELECT DISTINCT mes FROM lancamentos WHERE mes IS NOT NULL")
        rows = cur.fetchall()
        cur.close(); conn.close()

        ordem = {m: i for i, m in enumerate(MESES)}
        meses = sorted([r[0] for r in rows if r[0]], key=lambda m: ordem.get(m, 99))
        return jsonify({"meses": meses}), 200

    except Exception as e:
        return jsonify({"erro": str(e)}), 500


@app.route('/api/listar-categorias', methods=['GET'])
def listar_categorias():
    return jsonify({
        "entrada": ["Salário","Freelance","Investimentos","Outros (Entrada)"],
        "saida":   ["Alimentação","Moradia","Transporte","Saúde","Educação",
                    "Lazer","Vestuário","Contas/Serviços","Outros (Saída)"]
    }), 200


@app.route('/api/listar-pagamentos', methods=['GET'])
def listar_pagamentos():
    return jsonify({"pagamentos": [
        "Dinheiro","Cartão Débito","Cartão Crédito",
        "Transferência","Pix","Boleto","Débito Automático"
    ]}), 200


@app.route('/api/fixos', methods=['GET'])
def listar_fixos():
    try:
        conn = get_conn()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT id, tipo, descricao, valor, categoria, pagamento, 
                   parcelado, parcela_atual, parcela_total, data_inicio, ativo
            FROM fixos ORDER BY tipo, id
        """)
        rows = cur.fetchall()
        cur.close(); conn.close()
        
        entradas = []
        saidas = []
        for r in rows:
            item = {
                "id": r['id'],
                "descricao": r['descricao'],
                "valor": float(r['valor']),
                "categoria": r['categoria'],
                "pagamento": r['pagamento'],
                "parcelado": r['parcelado'],
                "parcela_atual": r['parcela_atual'],
                "parcela_total": r['parcela_total'],
                "data_inicio": r['data_inicio'],
                "ativo": r['ativo']
            }
            if r['tipo'] == 'ENTRADA':
                entradas.append(item)
            else:
                saidas.append(item)
        
        return jsonify({"entradas": entradas, "saidas": saidas}), 200
    except Exception as e:
        return jsonify({"erro": str(e)}), 500


@app.route('/api/fixos', methods=['POST'])
def adicionar_fixo():
    try:
        dados = request.json or {}
        
        # Validações
        for campo in ['tipo', 'descricao', 'valor', 'categoria']:
            if not str(dados.get(campo, '')).strip():
                return jsonify({"erro": f"Campo obrigatório: {campo}"}), 400
        
        tipo = dados['tipo'].upper()
        if tipo not in ['ENTRADA', 'SAÍDA']:
            return jsonify({"erro": "Tipo deve ser ENTRADA ou SAÍDA"}), 400
        
        parcelado = dados.get('parcelado', False)
        if parcelado:
            parcela_total = dados.get('parcela_total')
            data_inicio = dados.get('data_inicio', '')
            
            if not parcela_total or parcela_total < 2 or parcela_total > 120:
                return jsonify({"erro": "Parcelas devem estar entre 2 e 120"}), 400
            if not data_inicio or not data_inicio.strip():
                return jsonify({"erro": "Data de início é obrigatória para parcelados"}), 400
        
        conn = get_conn()
        cur  = conn.cursor()
        cur.execute("""
            INSERT INTO fixos (tipo, descricao, valor, categoria, pagamento, 
                              parcelado, parcela_atual, parcela_total, data_inicio, ativo)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE) RETURNING id
        """, (
            tipo, dados['descricao'].strip(),
            float(dados['valor']), dados.get('categoria',''),
            dados.get('pagamento',''), parcelado,
            1, dados.get('parcela_total', 1), dados.get('data_inicio', '')
        ))
        novo_id = cur.fetchone()[0]
        conn.commit(); cur.close(); conn.close()
        return jsonify({"sucesso": True, "id": novo_id}), 201
    except Exception as e:
        return jsonify({"erro": str(e)}), 500


@app.route('/api/fixos/<int:fixo_id>', methods=['PUT'])
def editar_fixo(fixo_id):
    try:
        dados = request.json or {}
        
        conn = get_conn()
        cur  = conn.cursor()
        
        # Busca o fixo atual
        cur.execute("SELECT * FROM fixos WHERE id=%s", (fixo_id,))
        fixo = cur.fetchone()
        if not fixo:
            cur.close(); conn.close()
            return jsonify({"erro": "Fixo não encontrado"}), 404
        
        # Atualiza campos fornecidos
        campos = []
        valores = []
        
        if 'descricao' in dados:
            campos.append("descricao=%s")
            valores.append(dados['descricao'])
        if 'valor' in dados:
            campos.append("valor=%s")
            valores.append(float(dados['valor']))
        if 'categoria' in dados:
            campos.append("categoria=%s")
            valores.append(dados['categoria'])
        if 'pagamento' in dados:
            campos.append("pagamento=%s")
            valores.append(dados['pagamento'])
        if 'parcelado' in dados:
            campos.append("parcelado=%s")
            valores.append(dados['parcelado'])
        if 'parcela_total' in dados:
            campos.append("parcela_total=%s")
            valores.append(dados['parcela_total'])
        if 'data_inicio' in dados:
            campos.append("data_inicio=%s")
            valores.append(dados['data_inicio'])
        
        if campos:
            valores.append(fixo_id)
            query = f"UPDATE fixos SET {', '.join(campos)} WHERE id=%s"
            cur.execute(query, valores)
            conn.commit()
        
        cur.close(); conn.close()
        return jsonify({"sucesso": True}), 200
    except Exception as e:
        return jsonify({"erro": str(e)}), 500


@app.route('/api/fixos/<int:fixo_id>', methods=['DELETE'])
def deletar_fixo(fixo_id):
    try:
        conn = get_conn()
        cur  = conn.cursor()
        cur.execute("DELETE FROM fixos WHERE id=%s", (fixo_id,))
        conn.commit(); cur.close(); conn.close()
        return jsonify({"sucesso": True}), 200
    except Exception as e:
        return jsonify({"erro": str(e)}), 500


@app.route('/api/fixos/<int:fixo_id>/toggle', methods=['PATCH'])
def toggle_fixo(fixo_id):
    try:
        conn = get_conn()
        cur  = conn.cursor()
        
        cur.execute("SELECT ativo FROM fixos WHERE id=%s", (fixo_id,))
        result = cur.fetchone()
        if not result:
            cur.close(); conn.close()
            return jsonify({"erro": "Fixo não encontrado"}), 404
        
        novo_ativo = not result[0]
        cur.execute("UPDATE fixos SET ativo=%s WHERE id=%s", (novo_ativo, fixo_id))
        conn.commit(); cur.close(); conn.close()
        
        return jsonify({"sucesso": True, "ativo": novo_ativo}), 200
    except Exception as e:
        return jsonify({"erro": str(e)}), 500


# ── Exportar Excel ────────────────────────────────────────────────────────────

@app.route('/api/exportar-excel', methods=['GET'])
def exportar_excel():
    try:
        mes_filtro = request.args.get('mes', '').strip()

        # Busca dados do banco
        conn = get_conn()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        if mes_filtro:
            cur.execute("""
                SELECT * FROM lancamentos WHERE LOWER(mes)=LOWER(%s)
                ORDER BY criado_em ASC
            """, (mes_filtro,))
        else:
            cur.execute("SELECT * FROM lancamentos ORDER BY criado_em ASC")

        lancamentos = [dict(r) for r in cur.fetchall()]
        cur.close(); conn.close()

        fixos = totais_fixos()
        renda_fixa   = fixos['ENTRADA']
        gastos_fixos = fixos['SAÍDA']

        total_entrada = sum(float(l['valor']) for l in lancamentos if l['tipo'] == 'ENTRADA')
        total_saida   = sum(float(l['valor']) for l in lancamentos if l['tipo'] == 'SAÍDA')
        if mes_filtro:
            total_entrada += renda_fixa
            total_saida   += gastos_fixos
        saldo = total_entrada - total_saida

        # Estilos
        def bdr():
            s = Side(style='thin', color='CBD5E1')
            return Border(top=s, bottom=s, left=s, right=s)
        def lfill(c): return PatternFill('solid', fgColor=c)
        def ca():     return Alignment(horizontal='center', vertical='center', wrap_text=True)
        def la():     return Alignment(horizontal='left',   vertical='center')
        def ra():     return Alignment(horizontal='right',  vertical='center')

        wb = Workbook()
        ws = wb.active
        ws.title = 'Lançamentos'

        for col, w in {'A':4,'B':14,'C':14,'D':32,'E':20,'F':12,'G':16,'H':22,'I':18}.items():
            ws.column_dimensions[col].width = w

        # Título
        titulo = f'CONTROLE FINANCEIRO — {mes_filtro.upper() if mes_filtro else "TODOS OS MESES"}'
        ws.row_dimensions[1].height = 44
        ws.merge_cells('A1:I1')
        c = ws['A1']; c.value = titulo
        c.font = Font(name='Arial', bold=True, color='FFFFFF', size=14)
        c.fill = lfill('1E293B'); c.alignment = ca()

        # Cards resumo
        ws.row_dimensions[2].height = 10
        ws.row_dimensions[3].height = 30
        for col_r, label, valor, bg, fc in [
            ('A3:C3','💚 ENTRADAS', total_entrada,'D1FAE5','065F46'),
            ('D3:F3','🔴 SAÍDAS',   total_saida,  'FEE2E2','7F1D1D'),
            ('G3:I3','💙 SALDO',    saldo,        'DBEAFE','1E3A8A'),
        ]:
            ws.merge_cells(col_r)
            c = ws[col_r.split(':')[0]]
            c.value = f'{label}:  R$ {valor:,.2f}'.replace(',','X').replace('.',',').replace('X','.')
            c.font = Font(name='Arial', bold=True, color=fc, size=11)
            c.fill = lfill(bg); c.alignment = ca(); c.border = bdr()

        ws.row_dimensions[4].height = 10

        # Cabeçalhos
        ws.row_dimensions[5].height = 28
        for ci, h in enumerate(['#','Mês','Data','Descrição','Categoria','Tipo','Valor (R$)','Forma Pagto.','Obs.'], 1):
            c = ws.cell(row=5, column=ci, value=h)
            c.font = Font(name='Arial', bold=True, color='FFFFFF', size=10)
            c.fill = lfill('1E293B'); c.alignment = ca(); c.border = bdr()

        # Linhas
        for ri, l in enumerate(lancamentos, 6):
            ws.row_dimensions[ri].height = 22
            rf  = lfill('F8FAFC') if ri % 2 == 0 else lfill('E2E8F0')
            ent = l['tipo'].upper() == 'ENTRADA'
            ct  = '065F46' if ent else '7F1D1D'
            for ci, v in enumerate([
                ri-5, l['mes'], l['data'], l['descricao'],
                l['categoria'], l['tipo'], float(l['valor']),
                l['pagamento'], l['obs']
            ], 1):
                c = ws.cell(row=ri, column=ci, value=v)
                c.fill = rf; c.border = bdr()
                if ci == 6:
                    c.font = Font(name='Arial', bold=True, color=ct, size=10); c.alignment = ca()
                elif ci == 7:
                    c.font = Font(name='Arial', bold=True, color=ct, size=10)
                    c.number_format = 'R$ #,##0.00'; c.alignment = ra()
                elif ci == 1:
                    c.font = Font(name='Arial', bold=True, size=10); c.alignment = ca()
                else:
                    c.font = Font(name='Arial', size=10)
                    c.alignment = la() if ci in (4,5,8,9) else ca()

        # Aba fixos
        ws2 = wb.create_sheet('Fixos Mensais')
        ws2.column_dimensions['A'].width = 30
        ws2.column_dimensions['B'].width = 20
        ws2.row_dimensions[1].height = 40
        ws2.merge_cells('A1:B1')
        c = ws2['A1']; c.value = 'RENDAS E GASTOS FIXOS MENSAIS'
        c.font = Font(name='Arial', bold=True, color='FFFFFF', size=13)
        c.fill = lfill('1E293B'); c.alignment = ca()
        for ri, (label, valor, bg, fc) in enumerate([
            ('💚 Renda Fixa',   renda_fixa,              'D1FAE5','065F46'),
            ('🔴 Gastos Fixos', gastos_fixos,            'FEE2E2','7F1D1D'),
            ('💙 Saldo Fixo',   renda_fixa-gastos_fixos, 'DBEAFE','1E3A8A'),
        ], 3):
            ws2.row_dimensions[ri].height = 28
            c = ws2.cell(row=ri, column=1, value=label)
            c.font = Font(name='Arial', bold=True, color=fc, size=11)
            c.fill = lfill(bg); c.alignment = la(); c.border = bdr()
            c = ws2.cell(row=ri, column=2, value=valor)
            c.number_format = 'R$ #,##0.00'
            c.font = Font(name='Arial', bold=True, color=fc, size=12)
            c.fill = lfill(bg); c.alignment = ra(); c.border = bdr()

        tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.xlsx')
        wb.save(tmp.name); tmp.close()

        data_hoje = datetime.now().strftime('%d-%m-%Y')
        nome = f'controle_{mes_filtro or "completo"}_{data_hoje}.xlsx'

        return send_file(tmp.name, as_attachment=True, download_name=nome,
                         mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

    except Exception as e:
        return jsonify({'erro': str(e)}), 500


# ── Servir frontend ───────────────────────────────────────────────────────────

@app.route('/')
def index():
    index_file = FRONTEND_DIR / 'index.html'
    if index_file.exists():
        return send_from_directory(str(FRONTEND_DIR), 'index.html')
    return jsonify({
        'api': 'online',
        'message': 'Frontend não está neste deploy. Use os endpoints /api/*.'
    }), 200

@app.route('/<path:filename>')
def static_files(filename):
    static_file = FRONTEND_DIR / filename
    if static_file.exists():
        return send_from_directory(str(FRONTEND_DIR), filename)
    return jsonify({'erro': 'Arquivo não encontrado'}), 404


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    print("\n" + "="*55)
    print("  💰 CONTROLE FINANCEIRO — INICIANDO")
    print("="*55)

    if not DATABASE_URL:
        print("  Configure a variável de ambiente no Railway.")
    else:
        print("  ✅ Banco de dados conectado")

    print("  🚀 Rodando em http://0.0.0.0:5000")
    print("="*55 + "\n")

    app.run(host='0.0.0.0', port=5000, debug=False)
