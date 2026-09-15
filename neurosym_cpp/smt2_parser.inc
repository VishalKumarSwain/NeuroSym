// ─────────────────────────── native SMT-LIB2 parser ─────────────────────────
// Replaces /tmp/dump_ir.py: turns SMT-LIB2 text directly into the same
// IR{nodes,assertions,declares} structure loadIR() builds from JSON, so the
// existing blastBool/blastBV backend is completely unchanged.
//
// Covers: declare-fun/declare-const (0-arity), define-fun (0-arity, as a
// let-like alias; non-nullary define-fun is parsed but not made callable --
// same limitation ns_parser.py itself has), assert, nested let (not let*;
// bindings in one let do not see each other, matching SMT-LIB2 semantics
// and gansat/ns_parser.py's own behaviour), BV literals in #xHH, #bBB and
// (_ bvN W) forms, (_ BitVec N) sorts, indexed ops ((_ extract hi lo) x),
// ((_ zero_extend n) x), ((_ sign_extend n) x), ((_ rotate_left/right n) x),
// ((_ repeat n) x), ite, and the ignorable commands set-logic/set-info/
// set-option/push/pop/check-sat/get-model/get-value/exit.
//
// Array theory: (Array (_ BitVec I) (_ BitVec E)) and Bool-valued-element
// arrays, select/store, and ((as const (Array I E)) v) are supported via a
// read-over-write encoding at bit-blast time (see blastSelect in
// bitblast_solver.cpp) -- ported from gansat/ns_bitblaster.py's
// _blast_select, including its iterative (non-recursive) store/ite-chain
// walk. Quantifiers (forall/exists) are skipped structurally like
// ns_parser.py does (not expected in QF_* logics).

#include <unordered_set>

// ── tokenizer ────────────────────────────────────────────────────────────

struct S2Token {
    enum Kind { LP, RP, SYM, BVLIT } kind;
    std::string text;
    unsigned long long val = 0;
    int width = 0;
};

static std::vector<S2Token> smt2Tokenize(const std::string &s) {
    std::vector<S2Token> toks;
    size_t i = 0, n = s.size();
    while (i < n) {
        char c = s[i];
        if (c == ';') { while (i < n && s[i] != '\n') i++; continue; }
        if (c == ' ' || c == '\t' || c == '\n' || c == '\r') { i++; continue; }
        if (c == '(') { toks.push_back({S2Token::LP}); i++; continue; }
        if (c == ')') { toks.push_back({S2Token::RP}); i++; continue; }
        if (c == '"') {
            size_t j = i + 1;
            while (j < n) {
                if (s[j] == '"') {
                    if (j + 1 < n && s[j + 1] == '"') j += 2; else break;
                } else j++;
            }
            S2Token t; t.kind = S2Token::SYM; t.text = "\"str\"";
            toks.push_back(t);
            i = j + 1;
            continue;
        }
        if (c == '|') {
            size_t j = s.find('|', i + 1);
            if (j == std::string::npos) j = n;
            S2Token t; t.kind = S2Token::SYM; t.text = s.substr(i + 1, j - i - 1);
            toks.push_back(t);
            i = j + 1;
            continue;
        }
        if (c == '#') {
            if (i + 1 < n && s[i + 1] == 'x') {
                size_t j = i + 2;
                while (j < n && isxdigit((unsigned char)s[j])) j++;
                std::string h = s.substr(i + 2, j - i - 2);
                S2Token t; t.kind = S2Token::BVLIT;
                t.val = strtoull(h.c_str(), nullptr, 16);
                t.width = (int)h.size() * 4;
                toks.push_back(t);
                i = j;
                continue;
            } else if (i + 1 < n && s[i + 1] == 'b') {
                size_t j = i + 2;
                while (j < n && (s[j] == '0' || s[j] == '1')) j++;
                std::string b = s.substr(i + 2, j - i - 2);
                S2Token t; t.kind = S2Token::BVLIT;
                t.val = strtoull(b.c_str(), nullptr, 2);
                t.width = (int)b.size();
                toks.push_back(t);
                i = j;
                continue;
            } else { i++; continue; }
        }
        if (c == ':') {
            size_t j = i + 1;
            while (j < n && s[j] != ' ' && s[j] != '\t' && s[j] != '\n' && s[j] != '\r' &&
                   s[j] != '(' && s[j] != ')' && s[j] != ';') j++;
            S2Token t; t.kind = S2Token::SYM; t.text = s.substr(i, j - i);
            toks.push_back(t);
            i = j;
            continue;
        }
        {
            size_t j = i;
            while (j < n && s[j] != ' ' && s[j] != '\t' && s[j] != '\n' && s[j] != '\r' &&
                   s[j] != '(' && s[j] != ')' && s[j] != '"' && s[j] != '#' && s[j] != ';' &&
                   s[j] != '|') j++;
            S2Token t; t.kind = S2Token::SYM; t.text = s.substr(i, j - i);
            toks.push_back(t);
            i = j;
        }
    }
    return toks;
}

struct S2Stream {
    const std::vector<S2Token> &t;
    size_t i = 0;
    S2Stream(const std::vector<S2Token> &tt) : t(tt) {}
    const S2Token &peek() const {
        static S2Token eof{S2Token::SYM, ""};
        if (i < t.size()) return t[i];
        return eof;
    }
    const S2Token &pop() { return t[i++]; }
    bool atEnd() const { return i >= t.size(); }
    void expectLP() { if (pop().kind != S2Token::LP) throw std::runtime_error("smt2: expected ("); }
    void expectRP() { if (pop().kind != S2Token::RP) throw std::runtime_error("smt2: expected )"); }
    void skipBalanced() { // already consumed the opening '(' of this form
        int depth = 1;
        while (depth) {
            if (atEnd()) throw std::runtime_error("smt2: unbalanced parens");
            auto k = pop().kind;
            if (k == S2Token::LP) depth++;
            else if (k == S2Token::RP) depth--;
        }
    }
};

// ── sort parsing: only Bool / (_ BitVec N) matter for bit-blasting ────────

struct S2Sort {
    bool isBool = false;
    int width = -1;
    // Array theory: isArray marks (Array (_ BitVec idxWidth) ELEM), with
    // ELEM described by elemIsBool/elemWidth the same way a scalar sort
    // would be (elemWidth meaningless when elemIsBool).
    bool isArray = false;
    int idxWidth = -1;
    bool elemIsBool = false;
    int elemWidth = -1;
};

static S2Sort smt2ParseSort(S2Stream &st) {
    const S2Token &t = st.peek();
    if (t.kind == S2Token::LP) {
        st.pop();
        const S2Token &head = st.pop();
        if (head.kind == S2Token::SYM && head.text == "_") {
            const S2Token &kind = st.pop();
            if (kind.text == "BitVec") {
                int w = std::stoi(st.pop().text);
                st.expectRP();
                return {false, w};
            }
            st.skipBalanced(); // consumes remaining tokens incl. the RP we already owe
            return {false, 1};
        } else if (head.kind == S2Token::SYM && head.text == "Array") {
            S2Sort idxS = smt2ParseSort(st);
            S2Sort elemS = smt2ParseSort(st);
            st.expectRP();
            if (idxS.isArray || idxS.isBool)
                throw std::runtime_error("smt2 parser: only BitVec-indexed arrays supported");
            if (elemS.isArray)
                throw std::runtime_error("smt2 parser: arrays of arrays not supported");
            S2Sort s;
            s.isBool = false; s.width = -1; s.isArray = true;
            s.idxWidth = idxS.width;
            s.elemIsBool = elemS.isBool;
            s.elemWidth = elemS.isBool ? 1 : elemS.width;
            return s;
        } else {
            st.skipBalanced();
            return {true, -1};
        }
    } else {
        const S2Token &s = st.pop();
        if (s.text == "Bool") return {true, -1};
        if (s.text == "Int") throw std::runtime_error("smt2 parser: Int sort not supported (QF_BV only)");
        return {true, -1};
    }
}

// ── term parsing into the shared IR node graph ─────────────────────────────

struct S2Env {
    std::vector<std::unordered_map<std::string, int>> scopes;
    int lookup(const std::string &name) const {
        for (auto it = scopes.rbegin(); it != scopes.rend(); ++it) {
            auto f = it->find(name);
            if (f != it->end()) return f->second;
        }
        return -1;
    }
};

struct S2Ctx {
    IR &ir;
    int trueNode = -1, falseNode = -1;
    int mkVar(const std::string &name, bool isBool, int width) {
        Node n; n.op = "var"; n.name = name; n.isBool = isBool; n.width = isBool ? -1 : width;
        ir.nodes.push_back(n);
        return (int)ir.nodes.size() - 1;
    }
    int mkArrayVar(const std::string &name, int idxWidth, bool elemIsBool, int elemWidth) {
        Node n; n.op = "var"; n.name = name; n.isBool = false; n.width = -1;
        n.isArray = true; n.idxWidth = idxWidth; n.elemIsBool = elemIsBool; n.elemWidth = elemWidth;
        ir.nodes.push_back(n);
        return (int)ir.nodes.size() - 1;
    }
    int mkBVLit(unsigned long long val, int width) {
        Node n; n.op = "bvlit"; n.value = (long long)val; n.width = width; n.isBool = false;
        ir.nodes.push_back(n);
        return (int)ir.nodes.size() - 1;
    }
    int mkTrue() {
        if (trueNode >= 0) return trueNode;
        Node n; n.op = "boollit"; n.boolValue = true; n.isBool = true; n.width = -1;
        ir.nodes.push_back(n);
        trueNode = (int)ir.nodes.size() - 1;
        return trueNode;
    }
    int mkFalse() {
        if (falseNode >= 0) return falseNode;
        Node n; n.op = "boollit"; n.boolValue = false; n.isBool = true; n.width = -1;
        ir.nodes.push_back(n);
        falseNode = (int)ir.nodes.size() - 1;
        return falseNode;
    }
};

static const std::unordered_set<std::string> S2_BOOL_OPS = {
    "and", "or", "not", "xor", "=>", "implies", "=", "distinct",
    "<", "<=", ">", ">=",
    "bvult", "bvule", "bvugt", "bvuge",
    "bvslt", "bvsle", "bvsgt", "bvsge",
};
static const std::unordered_set<std::string> S2_ARG0_OPS = {
    "bvadd", "bvsub", "bvmul",
    "bvsdiv", "bvudiv", "bvsrem", "bvurem", "bvsmod",
    "bvand", "bvor", "bvxor", "bvnand", "bvnor", "bvxnor",
    "bvshl", "bvlshr", "bvashr",
    "bvneg", "bvnot",
    "+", "-", "*", "div", "mod", "abs", "rem",
};

static S2Sort smt2InferSort(S2Ctx &ctx, const std::string &op, const std::vector<long long> &params,
                             const std::vector<int> &args) {
    if (op == "select") {
        // args = [array, index]; result is the array's element sort.
        const Node &arrN = ctx.ir.nodes[args[0]];
        S2Sort s; s.isBool = arrN.elemIsBool; s.width = arrN.elemWidth;
        return s;
    }
    if (op == "store") {
        // args = [array, index, value]; result is the same array sort.
        const Node &arrN = ctx.ir.nodes[args[0]];
        S2Sort s; s.isBool = false; s.width = -1; s.isArray = true;
        s.idxWidth = arrN.idxWidth; s.elemIsBool = arrN.elemIsBool; s.elemWidth = arrN.elemWidth;
        return s;
    }
    if (S2_BOOL_OPS.count(op)) return {true, -1};
    if (op == "bvcomp") return {false, 1};
    if (op == "concat") {
        int wa = ctx.ir.nodes[args[0]].isBool ? 0 : ctx.ir.nodes[args[0]].width;
        int wb = ctx.ir.nodes[args[1]].isBool ? 0 : ctx.ir.nodes[args[1]].width;
        return {false, wa + wb};
    }
    if (op == "extract") {
        long long hi = params[0], lo = params[1];
        return {false, (int)(hi - lo + 1)};
    }
    if (op == "zero_extend" || op == "sign_extend" || op == "repeat") {
        long long n = params[0];
        int w = (args.size() && !ctx.ir.nodes[args[0]].isBool) ? ctx.ir.nodes[args[0]].width : 0;
        if (op == "repeat") return {false, (int)(w * n)};
        return {false, (int)(w + n)};
    }
    if (op == "rotate_left" || op == "rotate_right") {
        if (args.empty()) return {false, 1};
        return {ctx.ir.nodes[args[0]].isBool, ctx.ir.nodes[args[0]].width};
    }
    if (op == "ite") {
        if (args.size() > 1) {
            const Node &tn = ctx.ir.nodes[args[1]];
            if (tn.isArray) {
                S2Sort s; s.isBool = false; s.width = -1; s.isArray = true;
                s.idxWidth = tn.idxWidth; s.elemIsBool = tn.elemIsBool; s.elemWidth = tn.elemWidth;
                return s;
            }
            return {tn.isBool, tn.width};
        }
        return {true, -1};
    }
    if (S2_ARG0_OPS.count(op) && !args.empty())
        return {ctx.ir.nodes[args[0]].isBool, ctx.ir.nodes[args[0]].width};
    if (!args.empty())
        return {ctx.ir.nodes[args[0]].isBool, ctx.ir.nodes[args[0]].width};
    return {true, -1};
}

static int smt2ParseTerm(S2Stream &st, S2Env &env, S2Ctx &ctx);

// Parses arguments after an op/params header has already been consumed, up
// to (and including) the closing RP.
static std::vector<int> smt2ParseArgList(S2Stream &st, S2Env &env, S2Ctx &ctx) {
    std::vector<int> args;
    while (st.peek().kind != S2Token::RP) args.push_back(smt2ParseTerm(st, env, ctx));
    st.expectRP();
    return args;
}

// Parses one term. `let`-chains are flattened iteratively: a real
// ESBMC-merged dump nests lets hundreds of thousands deep in a single
// assert (each subexpression gets its own let, chained:
// (let ((?x0 ..)) (let ((?x1 ..)) .. body))), and array-heavy formulas are
// exactly the kind that produce this. Recursing into the body once per let
// -- as this function used to -- makes the C++ call stack track let-nesting
// depth directly, which can blow the stack (a real crash here, not a
// catchable exception -- mirrors the segfault fixed in ns_parser.py's
// _parse_term, commit b6f56fa). The `for(;;)` loop below enters each
// nested let's scope and loops back to parse the body in place, instead of
// calling itself; letDepth counts how many scopes/close-parens are still
// owed once the eventual non-let body is parsed, unwound once at each exit
// point via `finish`. Only genuine sub-expression recursion remains
// (parsing a let's own bound value, ite's arms, operator arguments etc.) --
// that tracks ordinary expression depth, not let-nesting depth.
static int smt2ParseTerm(S2Stream &st, S2Env &env, S2Ctx &ctx) {
    int letDepth = 0;
    auto finish = [&](int r) -> int {
        while (letDepth--) { env.scopes.pop_back(); st.expectRP(); }
        return r;
    };

    for (;;) {
        const S2Token &t = st.peek();

        if (t.kind == S2Token::BVLIT) {
            st.pop();
            return finish(ctx.mkBVLit(t.val, t.width));
        }

        if (t.kind == S2Token::SYM) {
            // attribute keyword or string literal placeholder
            if (!t.text.empty() && (t.text[0] == ':' || t.text == "\"str\"")) {
                st.pop();
                return finish(ctx.mkTrue());
            }
            st.pop();
            if (t.text == "true") return finish(ctx.mkTrue());
            if (t.text == "false") return finish(ctx.mkFalse());
            // reject bare numerals -- this parser is QF_BV/QF_ABV only, same as
            // dump_ir.py which raises on IntLit
            {
                bool allDigits = !t.text.empty();
                size_t k = (t.text[0] == '-') ? 1 : 0;
                if (k >= t.text.size()) allDigits = false;
                for (size_t x = k; x < t.text.size() && allDigits; x++)
                    if (!isdigit((unsigned char)t.text[x])) allDigits = false;
                if (allDigits)
                    throw std::runtime_error("smt2 parser: bare integer literal '" + t.text +
                                              "' not supported (QF_BV/QF_ABV only)");
            }
            int nid = env.lookup(t.text);
            if (nid >= 0) return finish(nid);
            // unbound symbol -- treat as an opaque bool var, matching
            // ns_parser.py's fallback (Var(t, BOOL)); should not occur in
            // well-formed ESBMC output.
            return finish(ctx.mkVar(t.text, true, -1));
        }

        // compound term
        st.expectLP();
        if (st.peek().kind == S2Token::RP) { st.pop(); return finish(ctx.mkTrue()); }

        // let: enter the new scope and loop back to parse the body in
        // place instead of recursing (see function comment above).
        if (st.peek().kind == S2Token::SYM && st.peek().text == "let") {
            st.pop();
            st.expectLP();
            std::vector<std::pair<std::string, int>> bindings;
            while (st.peek().kind != S2Token::RP) {
                st.expectLP();
                std::string name = st.pop().text;
                int val = smt2ParseTerm(st, env, ctx); // sees OUTER scope only
                st.expectRP();
                bindings.emplace_back(name, val);
            }
            st.expectRP();
            env.scopes.emplace_back();
            for (auto &b : bindings) env.scopes.back()[b.first] = b.second;
            letDepth++;
            continue; // parse body next iteration, no recursive call
        }

        // forall/exists -- not expected in QF_*, skip the binder list, parse body
        if (st.peek().kind == S2Token::SYM && (st.peek().text == "forall" || st.peek().text == "exists")) {
            st.pop();
            st.expectLP();
            st.skipBalanced();
            int body = smt2ParseTerm(st, env, ctx);
            st.expectRP();
            return finish(body);
        }

        // ite
        if (st.peek().kind == S2Token::SYM && st.peek().text == "ite") {
            st.pop();
            int c = smt2ParseTerm(st, env, ctx);
            int th = smt2ParseTerm(st, env, ctx);
            int el = smt2ParseTerm(st, env, ctx);
            st.expectRP();
            std::vector<int> args = {c, th, el};
            S2Sort sort = smt2InferSort(ctx, "ite", {}, args);
            Node n; n.op = "ite"; n.args = args; n.isBool = sort.isBool; n.width = sort.width;
            n.isArray = sort.isArray; n.idxWidth = sort.idxWidth;
            n.elemIsBool = sort.elemIsBool; n.elemWidth = sort.elemWidth;
            ctx.ir.nodes.push_back(n);
            return finish((int)ctx.ir.nodes.size() - 1);
        }

        // ((as const (Array I E)) v) -- array-constant. The "(as const T)"
        // part is the function symbol; it is applied to one value argument
        // in an outer paren, so this is only reachable from the
        // "indexed op used as the head"-shaped branch below, but we handle
        // "as" directly here for the case it appears as the head itself.
        if (st.peek().kind == S2Token::SYM && st.peek().text == "as") {
            st.pop();
            std::string asHead = st.pop().text;
            S2Sort asSort = smt2ParseSort(st);
            st.expectRP(); // close "(as const T)"
            if (asHead != "const" || !asSort.isArray)
                throw std::runtime_error("smt2 parser: unsupported 'as' form '" + asHead + "'");
            throw std::runtime_error("smt2 parser: '(as const T)' used without a value argument");
        }

        // indexed op used as the head: ((_ op p...) args...), (_ bvN W) /
        // (_ BitVec N), or ((as const (Array I E)) v)
        if (st.peek().kind == S2Token::LP) {
            size_t saved = st.i;
            st.pop(); // inner '('
            if (st.peek().kind == S2Token::SYM && st.peek().text == "_") {
                st.pop(); // '_'
                std::string op = st.pop().text;
                std::vector<long long> params;
                while (st.peek().kind != S2Token::RP) params.push_back(std::stoll(st.pop().text));
                st.expectRP(); // close inner '('
                std::vector<int> args = smt2ParseArgList(st, env, ctx); // consumes outer RP
                S2Sort sort = smt2InferSort(ctx, op, params, args);
                Node n; n.op = op; n.args = args; n.params = params;
                n.isBool = sort.isBool; n.width = sort.width;
                n.isArray = sort.isArray; n.idxWidth = sort.idxWidth;
                n.elemIsBool = sort.elemIsBool; n.elemWidth = sort.elemWidth;
                ctx.ir.nodes.push_back(n);
                return finish((int)ctx.ir.nodes.size() - 1);
            }
            if (st.peek().kind == S2Token::SYM && st.peek().text == "as") {
                st.pop(); // 'as'
                std::string asHead = st.pop().text; // "const"
                S2Sort asSort = smt2ParseSort(st);
                st.expectRP(); // close inner "(as const T)"
                if (asHead != "const" || !asSort.isArray)
                    throw std::runtime_error("smt2 parser: unsupported 'as' form '" + asHead + "'");
                std::vector<int> args = smt2ParseArgList(st, env, ctx); // consumes outer RP
                Node n; n.op = "as_const"; n.args = {args[0]};
                n.isBool = false; n.width = -1; n.isArray = true;
                n.idxWidth = asSort.idxWidth; n.elemIsBool = asSort.elemIsBool; n.elemWidth = asSort.elemWidth;
                ctx.ir.nodes.push_back(n);
                return finish((int)ctx.ir.nodes.size() - 1);
            }
            st.i = saved; // not an indexed op after all; unsupported (function term as head)
            throw std::runtime_error("smt2 parser: nested non-indexed term as application head not supported");
        }

        // (_ bvN W) as a whole term, or other indexed constant
        if (st.peek().kind == S2Token::SYM && st.peek().text == "_") {
            st.pop();
            std::string sym = st.pop().text;
            if (sym.size() > 2 && sym[0] == 'b' && sym[1] == 'v') {
                unsigned long long val = strtoull(sym.c_str() + 2, nullptr, 10);
                int w = std::stoi(st.pop().text);
                st.expectRP();
                return finish(ctx.mkBVLit(val, w));
            }
            // unknown indexed constant (e.g. FP +oo) -- skip, placeholder true
            while (st.peek().kind != S2Token::RP) st.pop();
            st.expectRP();
            return finish(ctx.mkTrue());
        }

        // general operator application (includes select/store)
        std::string op = st.pop().text;
        std::vector<int> args = smt2ParseArgList(st, env, ctx); // consumes RP
        S2Sort sort = smt2InferSort(ctx, op, {}, args);
        Node n; n.op = op; n.args = args; n.isBool = sort.isBool; n.width = sort.width;
        n.isArray = sort.isArray; n.idxWidth = sort.idxWidth;
        n.elemIsBool = sort.elemIsBool; n.elemWidth = sort.elemWidth;
        ctx.ir.nodes.push_back(n);
        return finish((int)ctx.ir.nodes.size() - 1);
    }
}

static IR parseSmt2File(const std::string &path) {
    std::ifstream f(path);
    if (!f) throw std::runtime_error("cannot open " + path);
    std::stringstream ss; ss << f.rdbuf();
    std::string text = ss.str();

    std::vector<S2Token> toks = smt2Tokenize(text);
    S2Stream st(toks);
    IR ir;
    S2Ctx ctx{ir};
    S2Env env;
    env.scopes.emplace_back(); // global scope: declare-fun/const, nullary define-fun

    std::unordered_map<std::string, S2Sort> declaredSorts; // name -> sort, for the declares[] list

    while (!st.atEnd()) {
        if (st.peek().kind != S2Token::LP) { st.pop(); continue; }
        st.pop(); // '('
        std::string cmd = st.pop().text;

        if (cmd == "set-logic") { st.pop(); st.expectRP(); }
        else if (cmd == "declare-fun") {
            std::string name = st.pop().text;
            st.expectLP();
            std::vector<S2Sort> paramSorts;
            while (st.peek().kind != S2Token::RP) paramSorts.push_back(smt2ParseSort(st));
            st.expectRP();
            S2Sort ret = smt2ParseSort(st);
            st.expectRP();
            if (paramSorts.empty()) {
                int nid = ret.isArray ? ctx.mkArrayVar(name, ret.idxWidth, ret.elemIsBool, ret.elemWidth)
                                       : ctx.mkVar(name, ret.isBool, ret.width);
                env.scopes.front()[name] = nid;
                declaredSorts[name] = ret;
            } // non-nullary function decls: not callable, skip (matches ns_parser.py)
        }
        else if (cmd == "declare-const") {
            std::string name = st.pop().text;
            S2Sort ret = smt2ParseSort(st);
            st.expectRP();
            int nid = ret.isArray ? ctx.mkArrayVar(name, ret.idxWidth, ret.elemIsBool, ret.elemWidth)
                                   : ctx.mkVar(name, ret.isBool, ret.width);
            env.scopes.front()[name] = nid;
            declaredSorts[name] = ret;
        }
        else if (cmd == "define-fun") {
            std::string name = st.pop().text;
            st.expectLP();
            std::vector<std::pair<std::string, S2Sort>> params;
            while (st.peek().kind != S2Token::RP) {
                st.expectLP();
                std::string pname = st.pop().text;
                S2Sort psort = smt2ParseSort(st);
                st.expectRP();
                params.emplace_back(pname, psort);
            }
            st.expectRP();
            smt2ParseSort(st); // return sort (unchecked against body -- matches ns_parser.py)
            if (params.empty()) {
                int body = smt2ParseTerm(st, env, ctx);
                st.expectRP();
                env.scopes.front()[name] = body;
            } else {
                // parse body in a scope with fresh param vars so it doesn't
                // error out, but the definition itself is not made callable
                // (same limitation as ns_parser.py: only nullary define-fun
                // becomes usable).
                env.scopes.emplace_back();
                for (auto &p : params)
                    env.scopes.back()[p.first] =
                        p.second.isArray ? ctx.mkArrayVar(p.first, p.second.idxWidth, p.second.elemIsBool, p.second.elemWidth)
                                          : ctx.mkVar(p.first, p.second.isBool, p.second.width);
                smt2ParseTerm(st, env, ctx);
                env.scopes.pop_back();
                st.expectRP();
            }
        }
        else if (cmd == "assert") {
            int term = smt2ParseTerm(st, env, ctx);
            st.expectRP();
            ir.assertions.push_back(term);
        }
        else if (cmd == "check-sat" || cmd == "get-model" || cmd == "exit" ||
                 cmd == "get-value" || cmd == "get-unsat-core") {
            while (st.peek().kind != S2Token::RP) st.pop();
            st.pop();
        }
        else if (cmd == "set-info" || cmd == "set-option" || cmd == "push" || cmd == "pop") {
            st.skipBalanced();
        }
        else {
            st.skipBalanced();
        }
    }

    for (auto &kv : declaredSorts) {
        if (kv.second.isArray) continue; // no scalar model value to report for an array
        Declare d;
        d.name = kv.first;
        d.isBool = kv.second.isBool;
        d.width = kv.second.isBool ? -1 : kv.second.width;
        ir.declares.push_back(d);
    }
    // deterministic order (JSON path emitted declares sorted by name, via
    // Python's f.var_names = sorted(decl_vars.keys())), for output stability
    std::sort(ir.declares.begin(), ir.declares.end(),
              [](const Declare &a, const Declare &b) { return a.name < b.name; });

    return ir;
}
