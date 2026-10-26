// ─────────────────── interactive (push/pop) QF_BV engine ───────────────────
// Entry point: bitblast_solver --interactive. Reads SMT-LIB2 commands from
// stdin and answers on stdout, keeping one term graph, one bit-blasted
// circuit and one MiniSat Solver alive across all check-sat queries.
//
// Push/pop uses one selector literal s_L per pushed frame L. Every clause
// created while L is the top frame is added as (not s_L) OR C, each check
// assumes the selectors of ALL live frames, and pop adds the unit (not s_L)
// permanently, which satisfies every clause the frame ever created. Caches of
// gates and term bits are recorded per frame and dropped on pop, so a later
// sibling frame can never reuse a literal whose defining clauses are disabled.
//
// The batch path's preprocessing (word-level substitution, unconstrained-term
// elimination, SimpSolver variable elimination) is deliberately not used:
// none of it stays sound when a later assertion may mention the same terms
// again. See DESIGN.txt (push/pop design, 2026-10-26) sections 4-8.
//
// Included at the end of bitblast_solver.cpp; reuses parseBVDigits() and
// isPow2Const() from there. Batch mode is unchanged.

#include <cctype>
#include <memory>
#include <pthread.h>

namespace nsi {

// ── S-expressions ──────────────────────────────────────────────────────────

struct SExpr {
    enum Kind { ATOM, STRING, LIST } kind = ATOM;
    std::string text;      // atom/symbol text (without |bars|), or string body
    bool quoted = false;   // |quoted symbol|
    std::vector<SExpr> kids;

    bool isAtom(const char *s) const { return kind == ATOM && !quoted && text == s; }
};

static std::string show(const SExpr &e) {
    if (e.kind == SExpr::LIST) {
        std::string s = "(";
        for (size_t i = 0; i < e.kids.size(); i++) {
            if (i) s += ' ';
            s += show(e.kids[i]);
        }
        return s + ")";
    }
    if (e.kind == SExpr::STRING) {
        std::string s = "\"";
        for (char c : e.text) { if (c == '"') s += '"'; s += c; }
        return s + "\"";
    }
    return e.quoted ? "|" + e.text + "|" : e.text;
}

struct ParseError : std::runtime_error {
    using std::runtime_error::runtime_error;
};

// Streaming reader: returns after the closing ')' of a command without
// peeking further, so an interactive client gets its answer immediately.
class Reader {
public:
    explicit Reader(std::istream &in) : in(in) {}

    // false on clean end of input before a command starts
    bool read(SExpr &out) {
        skipWs();
        if (in.peek() == EOF) return false;
        out = parse();
        return true;
    }

private:
    std::istream &in;

    void skipWs() {
        for (;;) {
            int c = in.peek();
            if (c == EOF) return;
            if (std::isspace(c)) { in.get(); continue; }
            if (c == ';') {
                while (c != EOF && c != '\n') { in.get(); c = in.peek(); }
                continue;
            }
            return;
        }
    }

    SExpr parse() {
        skipWs();
        int c = in.get();
        if (c == EOF) throw ParseError("unexpected end of input inside a command");
        SExpr e;
        if (c == '(') {
            e.kind = SExpr::LIST;
            for (;;) {
                skipWs();
                int d = in.peek();
                if (d == EOF) throw ParseError("unexpected end of input inside a command");
                if (d == ')') { in.get(); break; }
                e.kids.push_back(parse());
            }
            return e;
        }
        if (c == ')') throw ParseError("unexpected ')'");
        if (c == '"') {
            e.kind = SExpr::STRING;
            for (;;) {
                int d = in.get();
                if (d == EOF) throw ParseError("unterminated string literal");
                if (d == '"') {
                    if (in.peek() == '"') { in.get(); e.text += '"'; continue; }
                    break;
                }
                e.text += (char)d;
            }
            return e;
        }
        if (c == '|') {
            e.quoted = true;
            for (;;) {
                int d = in.get();
                if (d == EOF) throw ParseError("unterminated quoted symbol");
                if (d == '|') break;
                e.text += (char)d;
            }
            return e;
        }
        e.text += (char)c;
        for (;;) {
            int d = in.peek();
            if (d == EOF || std::isspace(d) || d == '(' || d == ')' || d == ';' ||
                d == '"' || d == '|')
                break;
            e.text += (char)in.get();
        }
        return e;
    }
};

// ── terms ──────────────────────────────────────────────────────────────────

enum Op {
    OP_VAR, OP_PARAM, OP_BVLIT, OP_BOOLLIT,
    OP_NOT, OP_AND, OP_OR, OP_XOR, OP_IMPLIES, OP_EQ, OP_DISTINCT, OP_ITE,
    OP_BVNOT, OP_BVNEG, OP_BVAND, OP_BVOR, OP_BVXOR, OP_BVNAND, OP_BVNOR, OP_BVXNOR,
    OP_BVADD, OP_BVSUB, OP_BVMUL, OP_BVUDIV, OP_BVUREM, OP_BVSDIV, OP_BVSREM, OP_BVSMOD,
    OP_BVSHL, OP_BVLSHR, OP_BVASHR, OP_CONCAT, OP_EXTRACT, OP_ZEXT, OP_SEXT,
    OP_ROTL, OP_ROTR, OP_REPEAT, OP_BVCOMP,
    OP_ULT, OP_ULE, OP_UGT, OP_UGE, OP_SLT, OP_SLE, OP_SGT, OP_SGE
};

struct Sort {
    bool isBool = true;
    int width = 0;   // > 0 for bit-vectors
    bool operator==(const Sort &o) const { return isBool == o.isBool && width == o.width; }
    bool operator!=(const Sort &o) const { return !(*this == o); }
};

static std::string showSort(const Sort &s) {
    return s.isBool ? "Bool" : "(_ BitVec " + std::to_string(s.width) + ")";
}

struct Term {
    Op op;
    std::vector<int> args;
    std::vector<long long> params;
    Sort sort;
    std::string bits;   // OP_BVLIT: exact value, MSB-first '0'/'1'
    bool bval = false;  // OP_BOOLLIT
    int sym = -1;       // OP_VAR: SymbolUID; OP_PARAM: parameter index
    bool isConst = false;
};

// A bit-vector or Boolean value, MSB-first for bit-vectors.
struct Val {
    bool b = false;
    std::vector<uint8_t> v;
};

// ── exact bit-vector arithmetic on values (for get-value and constants) ────

namespace bvops {
using V = std::vector<uint8_t>;

static V zero(int w) { return V(w, 0); }
static V ones(int w) { return V(w, 1); }
static bool isZero(const V &a) { for (uint8_t x : a) if (x) return false; return true; }
static V bnot(const V &a) { V r(a.size()); for (size_t i = 0; i < a.size(); i++) r[i] = !a[i]; return r; }
static V add(const V &a, const V &b) {
    V r(a.size()); int c = 0;
    for (int i = (int)a.size() - 1; i >= 0; i--) { int s = a[i] + b[i] + c; r[i] = s & 1; c = s >> 1; }
    return r;
}
static V neg(const V &a) { V one = zero(a.size()); if (!a.empty()) one.back() = 1; return add(bnot(a), one); }
static V sub(const V &a, const V &b) { return add(a, neg(b)); }
static bool ult(const V &a, const V &b) {
    for (size_t i = 0; i < a.size(); i++) if (a[i] != b[i]) return a[i] < b[i];
    return false;
}
static bool slt(const V &a, const V &b) {
    if (a[0] != b[0]) return a[0] > b[0];  // negative < non-negative
    return ult(a, b);
}
static V shlBy(const V &a, size_t k) {
    size_t w = a.size(); V r(w, 0);
    for (size_t i = 0; i + k < w; i++) r[i] = a[i + k];
    return r;
}
static V mul(const V &a, const V &b) {
    size_t w = a.size(); V r = zero(w);
    for (size_t i = 0; i < w; i++)               // bit i from LSB of b
        if (b[w - 1 - i]) r = add(r, shlBy(a, i));
    return r;
}
static void udivrem(const V &a, const V &b, V &q, V &r) {
    size_t w = a.size();
    if (isZero(b)) { q = ones(w); r = a; return; }
    V rem(w + 1, 0), bext(w + 1, 0);
    for (size_t i = 0; i < w; i++) bext[i + 1] = b[i];
    q = zero(w);
    for (size_t i = 0; i < w; i++) {
        for (size_t k = 0; k < w; k++) rem[k] = rem[k + 1];
        rem[w] = a[i];
        if (!ult(rem, bext)) { rem = sub(rem, bext); q[i] = 1; }
    }
    r = V(rem.begin() + 1, rem.end());
}
static V udiv(const V &a, const V &b) { V q, r; udivrem(a, b, q, r); return q; }
static V urem(const V &a, const V &b) { V q, r; udivrem(a, b, q, r); return r; }
// SMT-LIB 2.6 definitions of the signed operations in terms of unsigned ones.
static V sdiv(const V &s, const V &t) {
    bool ms = s[0], mt = t[0];
    if (!ms && !mt) return udiv(s, t);
    if (ms && !mt) return neg(udiv(neg(s), t));
    if (!ms && mt) return neg(udiv(s, neg(t)));
    return udiv(neg(s), neg(t));
}
static V srem(const V &s, const V &t) {
    bool ms = s[0], mt = t[0];
    if (!ms && !mt) return urem(s, t);
    if (ms && !mt) return neg(urem(neg(s), t));
    if (!ms && mt) return urem(s, neg(t));
    return neg(urem(neg(s), neg(t)));
}
static V smod(const V &s, const V &t) {
    bool ms = s[0], mt = t[0];
    V u = urem(ms ? neg(s) : s, mt ? neg(t) : t);
    if (isZero(u)) return u;
    if (!ms && !mt) return u;
    if (ms && !mt) return add(neg(u), t);
    if (!ms && mt) return add(u, t);
    return neg(u);
}
// Shift amount as a size_t, saturated to w when the value is >= w.
static size_t shiftAmount(const V &b, size_t w) {
    size_t amt = 0;
    for (uint8_t bit : b) {
        if (amt > w) return w;      // already beyond the width
        amt = amt * 2 + bit;
    }
    return amt >= w ? w : amt;
}
static V shl(const V &a, const V &b) { return shlBy(a, shiftAmount(b, a.size())); }
static V lshr(const V &a, const V &b) {
    size_t w = a.size(), k = shiftAmount(b, w); V r(w, 0);
    for (size_t i = k; i < w; i++) r[i] = a[i - k];
    return r;
}
static V ashr(const V &a, const V &b) {
    size_t w = a.size(), k = shiftAmount(b, w); V r(w, a[0]);
    for (size_t i = k; i < w; i++) r[i] = a[i - k];
    return r;
}
} // namespace bvops

// ── the session ────────────────────────────────────────────────────────────

struct Frame {
    uint64_t uid = 0;
    int sel = 0;                         // 1-based selector literal; 0 = root
    std::vector<std::pair<int,int>> andKeys, xorKeys;
    std::vector<int> bvTerms, boolTerms;
    std::vector<std::string> names;      // bindings introduced in this frame
};

struct Macro {
    int nparams = 0;
    int body = -1;      // term id; parameters appear as OP_PARAM leaves
    Sort result;
};

struct Binding {
    bool isMacro = false;
    int id = -1;        // term id of the OP_VAR, or index into macros
};

struct SessionError : std::runtime_error {
    using std::runtime_error::runtime_error;
};

class Session {
public:
    explicit Session(std::ostream &out) : out(out) { reset(); }

    // Returns false when the session should end (exit command).
    bool command(const SExpr &cmd);

private:
    std::ostream &out;

    // SAT state
    std::unique_ptr<Solver> S;
    int constTrue = 0, constFalse = 0;

    // terms
    std::vector<Term> terms;
    std::unordered_map<std::string, int> termIndex;
    std::vector<Macro> macros;
    std::vector<Sort> symSorts;                       // by SymbolUID
    std::vector<std::string> symNames;                // by SymbolUID
    std::unordered_map<int, std::vector<int>> symBits; // SymbolUID -> primary bits
    std::unordered_map<std::string, std::vector<Binding>> bindings;
    std::vector<int> declOrder;                       // SymbolUIDs in declaration order

    // frames and frame-owned caches
    std::vector<Frame> frames;
    uint64_t nextFrameUid = 0;
    std::map<std::pair<int,int>, int> andCache, xorCache;
    std::unordered_map<int, std::vector<int>> bvCache;
    std::unordered_map<int, int> boolCache;

    // options and status
    bool printSuccess = false;
    bool produceModels = true;
    bool globalDecls = false;
    bool logicSet = false;
    bool poisoned = false;
    bool haveModel = false;
    std::vector<lbool> model;
    std::string lastUnknownReason = "incomplete";

    // ── responses ──
    void success() { if (printSuccess) out << "success\n"; out.flush(); }
    void error(const std::string &msg) {
        std::string m;
        for (char c : msg) { if (c == '"') m += '"'; m += c; }
        out << "(error \"" << m << "\")\n";
        out.flush();
    }
    void unsupported() { out << "unsupported\n"; out.flush(); }

    void reset();
    void invalidateModel() { haveModel = false; model.clear(); }

    // ── term construction ──
    int mk(Term t);
    int mkBool(bool v) { Term t; t.op = OP_BOOLLIT; t.sort = Sort{true, 0}; t.bval = v; return mk(t); }
    int mkBV(const std::string &bits) {
        Term t; t.op = OP_BVLIT; t.sort = Sort{false, (int)bits.size()}; t.bits = bits; return mk(t);
    }
    int mkOp(Op op, std::vector<int> args, std::vector<long long> params = {});
    Sort parseSort(const SExpr &e);
    int build(const SExpr &e, std::vector<std::unordered_map<std::string,int>> &env);
    int buildApp(const std::string &f, const std::vector<long long> &idx,
                 std::vector<int> args, const SExpr &e);
    int instantiate(int body, const std::vector<int> &actuals,
                    std::unordered_map<int,int> &memo);
    int buildTop(const SExpr &e) {
        std::vector<std::unordered_map<std::string,int>> env;
        return build(e, env);
    }

    // ── evaluation ──
    Val eval(int t, std::unordered_map<int, Val> &memo, bool useModel);
    bool constValue(int t, Val &v) {
        if (!terms[t].isConst) return false;
        std::unordered_map<int, Val> memo;
        v = eval(t, memo, false);
        return true;
    }

    // ── SAT layer: every clause goes through addClauseLits ──
    int fresh() { return (int)S->newVar() + 1; }
    static Lit toLit(int l) { return mkLit((l > 0 ? l : -l) - 1, l < 0); }
    void addClauseLits(const std::vector<int> &lits) {
        vec<Lit> ps;
        for (int l : lits) ps.push(toLit(l));
        int sel = frames.back().sel;
        if (sel) ps.push(toLit(-sel));
        S->addClause(ps);
    }
    int CONST_TRUE() const { return constTrue; }
    int CONST_FALSE() const { return constFalse; }

    // gates (same algorithms as the batch Blaster; caches are frame-owned)
    int gate_not(int a) { return -a; }
    int gate_and(int a, int b);
    int gate_or(int a, int b);
    int gate_xor(int a, int b);
    int gate_ite(int c, int t, int e);
    int gate_and_n(const std::vector<int> &bits);
    int gate_or_n(const std::vector<int> &bits);
    std::vector<int> bitsOfVal(const std::vector<uint8_t> &v);
    std::vector<int> int_to_bits(uint64_t val, int w);
    void full_adder(int a, int b, int cin, int &sum, int &cout);
    std::vector<int> bv_add(const std::vector<int> &a, const std::vector<int> &b);
    std::vector<int> bv_not(const std::vector<int> &a);
    std::vector<int> bv_neg(const std::vector<int> &a);
    std::vector<int> bv_sub(const std::vector<int> &a, const std::vector<int> &b);
    std::vector<int> bv_bitwise(Op op, const std::vector<int> &a, const std::vector<int> &b);
    std::vector<int> bv_mul(const std::vector<int> &a, const std::vector<int> &b);
    std::vector<int> bv_shl_const(const std::vector<int> &a, int k);
    std::vector<int> bv_lshr_const(const std::vector<int> &a, int k);
    std::vector<int> bv_urem_pow2_const(const std::vector<int> &a, int k);
    std::vector<int> bv_mul_by_sparse_const(const std::vector<int> &x, uint64_t c, int w);
    void bv_reldiv_by_const(const std::vector<int> &a, uint64_t c, int w,
                            std::vector<int> &q_out, std::vector<int> &r_out);
    int bv_eq(const std::vector<int> &a, const std::vector<int> &b);
    int bv_ult(const std::vector<int> &a, const std::vector<int> &b);
    int bv_ule(const std::vector<int> &a, const std::vector<int> &b) { return gate_not(bv_ult(b, a)); }
    int bv_slt(const std::vector<int> &a, const std::vector<int> &b);
    int bv_sle(const std::vector<int> &a, const std::vector<int> &b) { return gate_not(bv_slt(b, a)); }
    void bv_udivrem(const std::vector<int> &a, const std::vector<int> &b,
                    std::vector<int> &q_out, std::vector<int> &r_out);
    std::vector<int> bv_bvudiv(const std::vector<int> &a, const std::vector<int> &b);
    std::vector<int> bv_bvurem(const std::vector<int> &a, const std::vector<int> &b);
    std::vector<int> bv_bvsdiv(const std::vector<int> &a, const std::vector<int> &b);
    std::vector<int> bv_bvsrem(const std::vector<int> &a, const std::vector<int> &b);
    std::vector<int> bv_shift(Op op, const std::vector<int> &a, const std::vector<int> &b);
    bool smallConst(int t, uint64_t &cv);

    std::vector<int> blastBV(int t);
    int blastBool(int t);

    // ── commands ──
    void cmdDeclare(const std::string &name, const Sort &s);
    void cmdDefine(const SExpr &cmd);
    void cmdAssert(const SExpr &cmd);
    void cmdPush(long long n);
    void cmdPop(long long n);
    void cmdCheck(const std::vector<int> &assumptions);
    void cmdGetValue(const SExpr &cmd);
    void cmdGetModel();
    void bindName(const std::string &name, Binding b);
    std::string showVal(const Val &v, const Sort &s);
};

void Session::reset() {
    S.reset(new Solver());
    terms.clear();
    termIndex.clear();
    macros.clear();
    symSorts.clear();
    symNames.clear();
    symBits.clear();
    bindings.clear();
    declOrder.clear();
    frames.clear();
    andCache.clear();
    xorCache.clear();
    bvCache.clear();
    boolCache.clear();
    Frame root;
    root.uid = nextFrameUid++;
    frames.push_back(root);
    // Root constants with permanent defining units, created before any push.
    constTrue = fresh();
    addClauseLits({constTrue});
    constFalse = fresh();
    addClauseLits({-constFalse});
    printSuccess = false;
    produceModels = true;
    globalDecls = false;
    logicSet = false;
    poisoned = false;
    invalidateModel();
}

// ── term construction ──────────────────────────────────────────────────────

int Session::mk(Term t) {
    std::string key = std::to_string((int)t.op) + ":" + (t.sort.isBool ? "B" : std::to_string(t.sort.width)) + ":";
    for (long long p : t.params) key += std::to_string(p) + ",";
    key += ":";
    for (int a : t.args) key += std::to_string(a) + ",";
    key += ":" + std::to_string(t.sym) + ":" + t.bits + ":" + (t.bval ? "1" : "0");
    auto it = termIndex.find(key);
    if (it != termIndex.end()) return it->second;
    if (t.op == OP_BVLIT || t.op == OP_BOOLLIT) t.isConst = true;
    else if (t.op == OP_VAR || t.op == OP_PARAM) t.isConst = false;
    else {
        t.isConst = true;
        for (int a : t.args) if (!terms[a].isConst) { t.isConst = false; break; }
    }
    int id = (int)terms.size();
    terms.push_back(std::move(t));
    termIndex.emplace(std::move(key), id);
    return id;
}

static bool isBVOp(Op op) {
    switch (op) {
    case OP_BVNOT: case OP_BVNEG: case OP_BVAND: case OP_BVOR: case OP_BVXOR:
    case OP_BVNAND: case OP_BVNOR: case OP_BVXNOR: case OP_BVADD: case OP_BVSUB:
    case OP_BVMUL: case OP_BVUDIV: case OP_BVUREM: case OP_BVSDIV: case OP_BVSREM:
    case OP_BVSMOD: case OP_BVSHL: case OP_BVLSHR: case OP_BVASHR:
        return true;
    default:
        return false;
    }
}

int Session::mkOp(Op op, std::vector<int> args, std::vector<long long> params) {
    auto sortOf = [&](int a) -> const Sort & { return terms[a].sort; };
    auto needArgs = [&](size_t n) {
        if (args.size() != n) throw SessionError("wrong number of arguments");
    };
    auto needBool = [&](int a) {
        if (!sortOf(a).isBool) throw SessionError("expected a Bool argument");
    };
    auto needBV = [&](int a) {
        if (sortOf(a).isBool) throw SessionError("expected a bit-vector argument");
    };
    Term t;
    t.op = op;
    t.params = params;
    switch (op) {
    case OP_NOT:
        needArgs(1); needBool(args[0]);
        t.sort = Sort{true, 0};
        break;
    case OP_AND: case OP_OR: case OP_XOR: case OP_IMPLIES:
        if (args.size() < 2) throw SessionError("expected at least two arguments");
        for (int a : args) needBool(a);
        if (op == OP_XOR && args.size() > 2) {        // left-associative
            int acc = mkOp(OP_XOR, {args[0], args[1]});
            for (size_t i = 2; i < args.size(); i++) acc = mkOp(OP_XOR, {acc, args[i]});
            return acc;
        }
        if (op == OP_IMPLIES && args.size() > 2) {    // right-associative
            int acc = args.back();
            for (int i = (int)args.size() - 2; i >= 0; i--) acc = mkOp(OP_IMPLIES, {args[i], acc});
            return acc;
        }
        t.sort = Sort{true, 0};
        break;
    case OP_EQ: case OP_DISTINCT:
        if (args.size() < 2) throw SessionError("expected at least two arguments");
        for (int a : args)
            if (sortOf(a) != sortOf(args[0])) throw SessionError("sort mismatch in = / distinct");
        if (op == OP_EQ && args.size() > 2) {          // chainable
            std::vector<int> conj;
            for (size_t i = 0; i + 1 < args.size(); i++) conj.push_back(mkOp(OP_EQ, {args[i], args[i + 1]}));
            return mkOp(OP_AND, conj);
        }
        t.sort = Sort{true, 0};
        break;
    case OP_ITE:
        needArgs(3); needBool(args[0]);
        if (sortOf(args[1]) != sortOf(args[2])) throw SessionError("sort mismatch in ite");
        t.sort = sortOf(args[1]);
        break;
    case OP_BVNOT: case OP_BVNEG:
        needArgs(1); needBV(args[0]);
        t.sort = sortOf(args[0]);
        break;
    case OP_BVAND: case OP_BVOR: case OP_BVXOR: case OP_BVADD: case OP_BVMUL:
        if (args.size() < 2) throw SessionError("expected at least two arguments");
        for (int a : args) {
            needBV(a);
            if (sortOf(a) != sortOf(args[0])) throw SessionError("bit-vector width mismatch");
        }
        if (args.size() > 2) {                         // left-associative
            int acc = mkOp(op, {args[0], args[1]});
            for (size_t i = 2; i < args.size(); i++) acc = mkOp(op, {acc, args[i]});
            return acc;
        }
        t.sort = sortOf(args[0]);
        break;
    case OP_BVNAND: case OP_BVNOR: case OP_BVXNOR: case OP_BVSUB:
    case OP_BVUDIV: case OP_BVUREM: case OP_BVSDIV: case OP_BVSREM: case OP_BVSMOD:
    case OP_BVSHL: case OP_BVLSHR: case OP_BVASHR: case OP_BVCOMP:
        needArgs(2); needBV(args[0]); needBV(args[1]);
        if (sortOf(args[0]) != sortOf(args[1])) throw SessionError("bit-vector width mismatch");
        t.sort = op == OP_BVCOMP ? Sort{false, 1} : sortOf(args[0]);
        break;
    case OP_ULT: case OP_ULE: case OP_UGT: case OP_UGE:
    case OP_SLT: case OP_SLE: case OP_SGT: case OP_SGE:
        needArgs(2); needBV(args[0]); needBV(args[1]);
        if (sortOf(args[0]) != sortOf(args[1])) throw SessionError("bit-vector width mismatch");
        t.sort = Sort{true, 0};
        break;
    case OP_CONCAT:
        if (args.size() < 2) throw SessionError("expected at least two arguments");
        for (int a : args) needBV(a);
        if (args.size() > 2) {
            int acc = mkOp(OP_CONCAT, {args[0], args[1]});
            for (size_t i = 2; i < args.size(); i++) acc = mkOp(OP_CONCAT, {acc, args[i]});
            return acc;
        }
        t.sort = Sort{false, sortOf(args[0]).width + sortOf(args[1]).width};
        break;
    case OP_EXTRACT: {
        needArgs(1); needBV(args[0]);
        long long hi = params[0], lo = params[1], w = sortOf(args[0]).width;
        if (lo < 0 || hi < lo || hi >= w) throw SessionError("invalid extract indices");
        t.sort = Sort{false, (int)(hi - lo + 1)};
        break;
    }
    case OP_ZEXT: case OP_SEXT: case OP_REPEAT: {
        needArgs(1); needBV(args[0]);
        long long k = params[0], w = sortOf(args[0]).width;
        if (k < 0 || (op == OP_REPEAT && k < 1)) throw SessionError("invalid index");
        long long nw = op == OP_REPEAT ? w * k : w + k;
        if (nw > (1LL << 24)) throw SessionError("bit-vector too wide");
        t.sort = Sort{false, (int)nw};
        break;
    }
    case OP_ROTL: case OP_ROTR:
        needArgs(1); needBV(args[0]);
        if (params[0] < 0) throw SessionError("invalid index");
        t.sort = sortOf(args[0]);
        break;
    default:
        throw SessionError("internal error: bad operator");
    }
    t.args = std::move(args);
    return mk(std::move(t));
}

Sort Session::parseSort(const SExpr &e) {
    if (e.isAtom("Bool")) return Sort{true, 0};
    if (e.kind == SExpr::LIST && e.kids.size() == 3 && e.kids[0].isAtom("_") &&
        e.kids[1].isAtom("BitVec") && e.kids[2].kind == SExpr::ATOM) {
        long long w = std::stoll(e.kids[2].text);
        if (w < 1 || w > (1LL << 24)) throw SessionError("invalid bit-vector width");
        return Sort{false, (int)w};
    }
    throw SessionError("unsupported sort " + show(e) + " (only Bool and (_ BitVec n))");
}

static bool isNumeral(const std::string &s) {
    if (s.empty()) return false;
    for (char c : s) if (!std::isdigit((unsigned char)c)) return false;
    return true;
}

int Session::buildApp(const std::string &f, const std::vector<long long> &idx,
                      std::vector<int> args, const SExpr &e) {
    static const std::unordered_map<std::string, Op> plain = {
        {"not", OP_NOT}, {"and", OP_AND}, {"or", OP_OR}, {"xor", OP_XOR},
        {"=>", OP_IMPLIES}, {"=", OP_EQ}, {"distinct", OP_DISTINCT}, {"ite", OP_ITE},
        {"bvnot", OP_BVNOT}, {"bvneg", OP_BVNEG}, {"bvand", OP_BVAND}, {"bvor", OP_BVOR},
        {"bvxor", OP_BVXOR}, {"bvnand", OP_BVNAND}, {"bvnor", OP_BVNOR}, {"bvxnor", OP_BVXNOR},
        {"bvadd", OP_BVADD}, {"bvsub", OP_BVSUB}, {"bvmul", OP_BVMUL}, {"bvudiv", OP_BVUDIV},
        {"bvurem", OP_BVUREM}, {"bvsdiv", OP_BVSDIV}, {"bvsrem", OP_BVSREM}, {"bvsmod", OP_BVSMOD},
        {"bvshl", OP_BVSHL}, {"bvlshr", OP_BVLSHR}, {"bvashr", OP_BVASHR}, {"concat", OP_CONCAT},
        {"bvcomp", OP_BVCOMP}, {"bvult", OP_ULT}, {"bvule", OP_ULE}, {"bvugt", OP_UGT},
        {"bvuge", OP_UGE}, {"bvslt", OP_SLT}, {"bvsle", OP_SLE}, {"bvsgt", OP_SGT},
        {"bvsge", OP_SGE}};
    static const std::unordered_map<std::string, std::pair<Op, size_t>> indexed = {
        {"extract", {OP_EXTRACT, 2}}, {"zero_extend", {OP_ZEXT, 1}}, {"sign_extend", {OP_SEXT, 1}},
        {"rotate_left", {OP_ROTL, 1}}, {"rotate_right", {OP_ROTR, 1}}, {"repeat", {OP_REPEAT, 1}}};
    if (!idx.empty()) {
        auto it = indexed.find(f);
        if (it == indexed.end() || it->second.second != idx.size())
            throw SessionError("unsupported indexed operator in " + show(e));
        return mkOp(it->second.first, std::move(args), idx);
    }
    auto it = plain.find(f);
    if (it != plain.end()) return mkOp(it->second, std::move(args));
    // user-defined function (define-fun with parameters)
    auto bit = bindings.find(f);
    if (bit != bindings.end() && !bit->second.empty() && bit->second.back().isMacro) {
        const Macro &m = macros[bit->second.back().id];
        if ((int)args.size() != m.nparams) throw SessionError("wrong number of arguments to " + f);
        std::unordered_map<int,int> memo;
        return instantiate(m.body, args, memo);
    }
    throw SessionError("unknown or unsupported function " + f);
}

int Session::instantiate(int body, const std::vector<int> &actuals,
                         std::unordered_map<int,int> &memo) {
    auto it = memo.find(body);
    if (it != memo.end()) return it->second;
    const Term t = terms[body];   // copy: the recursive mk() calls may grow terms
    int r;
    if (t.op == OP_PARAM) {
        r = actuals[t.sym];
        if (terms[r].sort != t.sort) throw SessionError("argument sort mismatch in function application");
    } else if (t.args.empty()) {
        r = body;
    } else {
        std::vector<int> nargs;
        for (int a : t.args) nargs.push_back(instantiate(a, actuals, memo));
        Term nt = t;
        nt.args = nargs;
        r = mk(nt);
    }
    memo[body] = r;
    return r;
}

int Session::build(const SExpr &e, std::vector<std::unordered_map<std::string,int>> &env) {
    if (e.kind == SExpr::STRING) throw SessionError("string literals are not supported");
    if (e.kind == SExpr::ATOM) {
        const std::string &s = e.text;
        if (!e.quoted) {
            if (s == "true") return mkBool(true);
            if (s == "false") return mkBool(false);
            if (s.size() > 2 && s[0] == '#' && s[1] == 'b') {
                std::string bits = s.substr(2);
                for (char c : bits) if (c != '0' && c != '1') throw SessionError("bad binary literal " + s);
                return mkBV(bits);
            }
            if (s.size() > 2 && s[0] == '#' && s[1] == 'x') {
                std::string hex = s.substr(2);
                return mkBV(parseBVDigits(hex, 16, (int)hex.size() * 4));
            }
        }
        for (auto it = env.rbegin(); it != env.rend(); ++it) {
            auto f = it->find(s);
            if (f != it->end()) return f->second;
        }
        auto bit = bindings.find(s);
        if (bit == bindings.end() || bit->second.empty()) throw SessionError("unknown symbol " + show(e));
        const Binding &b = bit->second.back();
        if (!b.isMacro) return b.id;
        const Macro &m = macros[b.id];
        if (m.nparams != 0) throw SessionError("function " + s + " used without arguments");
        return m.body;
    }
    // list
    if (e.kids.empty()) throw SessionError("empty term");
    const SExpr &h = e.kids[0];
    // (_ bvN w)
    if (h.isAtom("_")) {
        if (e.kids.size() == 3 && e.kids[1].kind == SExpr::ATOM && e.kids[1].text.rfind("bv", 0) == 0 &&
            isNumeral(e.kids[1].text.substr(2)) && e.kids[2].kind == SExpr::ATOM && isNumeral(e.kids[2].text)) {
            long long w = std::stoll(e.kids[2].text);
            if (w < 1 || w > (1LL << 24)) throw SessionError("invalid bit-vector width");
            return mkBV(parseBVDigits(e.kids[1].text.substr(2), 10, (int)w));
        }
        throw SessionError("unsupported term " + show(e));
    }
    if (h.isAtom("let")) {
        if (e.kids.size() != 3 || e.kids[1].kind != SExpr::LIST) throw SessionError("malformed let");
        std::unordered_map<std::string,int> scope;
        for (const SExpr &b : e.kids[1].kids) {
            if (b.kind != SExpr::LIST || b.kids.size() != 2 || b.kids[0].kind != SExpr::ATOM)
                throw SessionError("malformed let binding");
            scope[b.kids[0].text] = build(b.kids[1], env);   // parallel: outer env
        }
        env.push_back(std::move(scope));
        int r;
        try { r = build(e.kids[2], env); } catch (...) { env.pop_back(); throw; }
        env.pop_back();
        return r;
    }
    if (h.isAtom("!")) {
        if (e.kids.size() < 2) throw SessionError("malformed annotation");
        int r = build(e.kids[1], env);
        for (size_t i = 2; i + 1 < e.kids.size(); i += 2)
            if (e.kids[i].isAtom(":named")) {
                Macro m; m.nparams = 0; m.body = r; m.result = terms[r].sort;
                macros.push_back(m);
                bindName(e.kids[i + 1].text, Binding{true, (int)macros.size() - 1});
            }
        return r;
    }
    if (h.isAtom("forall") || h.isAtom("exists")) throw SessionError("quantifiers are not supported");
    std::vector<long long> idx;
    std::string f;
    if (h.kind == SExpr::LIST) {
        // ((_ extract i j) t) etc.
        if (h.kids.size() < 2 || !h.kids[0].isAtom("_") || h.kids[1].kind != SExpr::ATOM)
            throw SessionError("unsupported term " + show(e));
        f = h.kids[1].text;
        for (size_t i = 2; i < h.kids.size(); i++) {
            if (h.kids[i].kind != SExpr::ATOM || !isNumeral(h.kids[i].text))
                throw SessionError("bad index in " + show(h));
            idx.push_back(std::stoll(h.kids[i].text));
        }
    } else if (h.kind == SExpr::ATOM) {
        f = h.text;
    } else {
        throw SessionError("unsupported term " + show(e));
    }
    std::vector<int> args;
    for (size_t i = 1; i < e.kids.size(); i++) args.push_back(build(e.kids[i], env));
    return buildApp(f, idx, std::move(args), e);
}

void Session::bindName(const std::string &name, Binding b) {
    auto &stack = bindings[name];
    if (!stack.empty()) throw SessionError("symbol " + name + " already declared");
    stack.push_back(b);
    (globalDecls ? frames.front() : frames.back()).names.push_back(name);
}

// ── evaluation (exact, arbitrary width) ────────────────────────────────────

Val Session::eval(int t, std::unordered_map<int, Val> &memo, bool useModel) {
    auto it = memo.find(t);
    if (it != memo.end()) return it->second;
    using namespace bvops;
    const Term &n = terms[t];
    Val r;
    auto bv = [&](int i) { return eval(n.args[i], memo, useModel).v; };
    auto bl = [&](int i) { return eval(n.args[i], memo, useModel).b; };
    switch (n.op) {
    case OP_VAR: {
        auto sb = symBits.find(n.sym);
        if (n.sort.isBool) {
            r.b = false;
            if (useModel && sb != symBits.end()) {
                int l = sb->second[0];
                r.b = (size_t)(l - 1) < model.size() && model[l - 1] == l_True;
            }
        } else {
            r.v.assign(n.sort.width, 0);
            if (useModel && sb != symBits.end())
                for (int i = 0; i < n.sort.width; i++) {
                    int l = sb->second[i];
                    r.v[i] = (size_t)(l - 1) < model.size() && model[l - 1] == l_True;
                }
        }
        break;
    }
    case OP_PARAM: throw SessionError("internal error: unbound parameter");
    case OP_BVLIT: for (char c : n.bits) r.v.push_back(c == '1'); break;
    case OP_BOOLLIT: r.b = n.bval; break;
    case OP_NOT: r.b = !bl(0); break;
    case OP_AND: r.b = true; for (size_t i = 0; i < n.args.size(); i++) r.b = r.b && bl(i); break;
    case OP_OR: r.b = false; for (size_t i = 0; i < n.args.size(); i++) r.b = r.b || bl(i); break;
    case OP_XOR: r.b = bl(0) != bl(1); break;
    case OP_IMPLIES: r.b = !bl(0) || bl(1); break;
    case OP_EQ:
        if (terms[n.args[0]].sort.isBool) r.b = bl(0) == bl(1);
        else r.b = bv(0) == bv(1);
        break;
    case OP_DISTINCT: {
        std::vector<Val> vs;
        for (size_t i = 0; i < n.args.size(); i++) vs.push_back(eval(n.args[i], memo, useModel));
        r.b = true;
        for (size_t i = 0; i < vs.size(); i++)
            for (size_t j = i + 1; j < vs.size(); j++)
                if (vs[i].b == vs[j].b && vs[i].v == vs[j].v) r.b = false;
        break;
    }
    case OP_ITE: r = bl(0) ? eval(n.args[1], memo, useModel) : eval(n.args[2], memo, useModel); break;
    case OP_BVNOT: r.v = bnot(bv(0)); break;
    case OP_BVNEG: r.v = neg(bv(0)); break;
    case OP_BVAND: case OP_BVOR: case OP_BVXOR: case OP_BVNAND: case OP_BVNOR: case OP_BVXNOR: {
        V a = bv(0), b = bv(1);
        r.v.resize(a.size());
        for (size_t i = 0; i < a.size(); i++) {
            uint8_t x = n.op == OP_BVAND || n.op == OP_BVNAND ? (a[i] & b[i])
                      : n.op == OP_BVOR || n.op == OP_BVNOR ? (a[i] | b[i]) : (a[i] ^ b[i]);
            if (n.op == OP_BVNAND || n.op == OP_BVNOR || n.op == OP_BVXNOR) x = !x;
            r.v[i] = x;
        }
        break;
    }
    case OP_BVADD: r.v = add(bv(0), bv(1)); break;
    case OP_BVSUB: r.v = sub(bv(0), bv(1)); break;
    case OP_BVMUL: r.v = mul(bv(0), bv(1)); break;
    case OP_BVUDIV: r.v = udiv(bv(0), bv(1)); break;
    case OP_BVUREM: r.v = urem(bv(0), bv(1)); break;
    case OP_BVSDIV: r.v = sdiv(bv(0), bv(1)); break;
    case OP_BVSREM: r.v = srem(bv(0), bv(1)); break;
    case OP_BVSMOD: r.v = smod(bv(0), bv(1)); break;
    case OP_BVSHL: r.v = shl(bv(0), bv(1)); break;
    case OP_BVLSHR: r.v = lshr(bv(0), bv(1)); break;
    case OP_BVASHR: r.v = ashr(bv(0), bv(1)); break;
    case OP_CONCAT: { r.v = bv(0); V b = bv(1); r.v.insert(r.v.end(), b.begin(), b.end()); break; }
    case OP_EXTRACT: {
        V a = bv(0); size_t w = a.size();
        r.v.assign(a.begin() + (w - 1 - n.params[0]), a.begin() + (w - n.params[1]));
        break;
    }
    case OP_ZEXT: { V a = bv(0); r.v.assign(n.params[0], 0); r.v.insert(r.v.end(), a.begin(), a.end()); break; }
    case OP_SEXT: { V a = bv(0); r.v.assign(n.params[0], a[0]); r.v.insert(r.v.end(), a.begin(), a.end()); break; }
    case OP_ROTL: case OP_ROTR: {
        V a = bv(0); size_t w = a.size(); size_t k = (size_t)(n.params[0] % (long long)w);
        r.v.resize(w);
        for (size_t i = 0; i < w; i++)
            r.v[i] = n.op == OP_ROTL ? a[(i + k) % w] : a[(i + w - k) % w];
        break;
    }
    case OP_REPEAT: { V a = bv(0); for (long long i = 0; i < n.params[0]; i++) r.v.insert(r.v.end(), a.begin(), a.end()); break; }
    case OP_BVCOMP: r.v = {(uint8_t)(bv(0) == bv(1))}; break;
    case OP_ULT: r.b = ult(bv(0), bv(1)); break;
    case OP_ULE: r.b = !ult(bv(1), bv(0)); break;
    case OP_UGT: r.b = ult(bv(1), bv(0)); break;
    case OP_UGE: r.b = !ult(bv(0), bv(1)); break;
    case OP_SLT: r.b = slt(bv(0), bv(1)); break;
    case OP_SLE: r.b = !slt(bv(1), bv(0)); break;
    case OP_SGT: r.b = slt(bv(1), bv(0)); break;
    case OP_SGE: r.b = !slt(bv(0), bv(1)); break;
    }
    memo[t] = r;
    return r;
}

// ── gates (frame-owned caches) ─────────────────────────────────────────────

int Session::gate_and(int a, int b) {
    int T = CONST_TRUE(), F = CONST_FALSE();
    if (a == F || b == F || a == -T || b == -T) return F;
    if (a == T || a == -F) return b;
    if (b == T || b == -F) return a;
    if (a == b) return a;
    if (a == -b) return F;
    std::pair<int,int> key = a <= b ? std::make_pair(a, b) : std::make_pair(b, a);
    auto it = andCache.find(key);
    if (it != andCache.end()) return it->second;
    int y = fresh();
    addClauseLits({-y, a});
    addClauseLits({-y, b});
    addClauseLits({y, -a, -b});
    andCache[key] = y;
    frames.back().andKeys.push_back(key);
    return y;
}

int Session::gate_or(int a, int b) { return -gate_and(-a, -b); }

int Session::gate_xor(int a, int b) {
    int T = CONST_TRUE(), F = CONST_FALSE();
    if (a == F || a == -T) return b;
    if (b == F || b == -T) return a;
    if (a == T || a == -F) return -b;
    if (b == T || b == -F) return -a;
    if (a == b) return F;
    if (a == -b) return T;
    std::pair<int,int> key = a <= b ? std::make_pair(a, b) : std::make_pair(b, a);
    auto it = xorCache.find(key);
    if (it != xorCache.end()) return it->second;
    int y = fresh();
    addClauseLits({-y, -a, -b});
    addClauseLits({-y, a, b});
    addClauseLits({y, -a, b});
    addClauseLits({y, a, -b});
    xorCache[key] = y;
    frames.back().xorKeys.push_back(key);
    return y;
}

int Session::gate_ite(int c, int t, int e) {
    int T = CONST_TRUE(), F = CONST_FALSE();
    if (c == T || c == -F) return t;
    if (c == F || c == -T) return e;
    if (t == e) return t;
    if ((t == T || t == -F) && (e == F || e == -T)) return c;
    if ((t == F || t == -T) && (e == T || e == -F)) return -c;
    int y = fresh();
    addClauseLits({-y, -c, t});
    addClauseLits({-y, c, e});
    addClauseLits({y, -c, -t});
    addClauseLits({y, c, -e});
    return y;
}

int Session::gate_and_n(const std::vector<int> &bits) {
    if (bits.empty()) return CONST_TRUE();
    int acc = bits[0];
    for (size_t i = 1; i < bits.size(); i++) acc = gate_and(acc, bits[i]);
    return acc;
}

int Session::gate_or_n(const std::vector<int> &bits) {
    if (bits.empty()) return CONST_FALSE();
    int acc = bits[0];
    for (size_t i = 1; i < bits.size(); i++) acc = gate_or(acc, bits[i]);
    return acc;
}

std::vector<int> Session::bitsOfVal(const std::vector<uint8_t> &v) {
    std::vector<int> r(v.size());
    for (size_t i = 0; i < v.size(); i++) r[i] = v[i] ? CONST_TRUE() : CONST_FALSE();
    return r;
}

std::vector<int> Session::int_to_bits(uint64_t val, int w) {
    std::vector<int> bits(w);
    for (int i = 0; i < w; i++) {
        int shift = w - 1 - i;
        bool bit = shift < 64 && ((val >> shift) & 1ULL);
        bits[i] = bit ? CONST_TRUE() : CONST_FALSE();
    }
    return bits;
}

void Session::full_adder(int a, int b, int cin, int &sum, int &cout) {
    int ab = gate_xor(a, b);
    sum = gate_xor(ab, cin);
    cout = gate_or(gate_and(a, b), gate_and(cin, ab));
}

std::vector<int> Session::bv_add(const std::vector<int> &a, const std::vector<int> &b) {
    int w = (int)a.size();
    std::vector<int> sums(w);
    int carry = CONST_FALSE();
    for (int i = w - 1; i >= 0; i--) {
        int s, c;
        full_adder(a[i], b[i], carry, s, c);
        sums[i] = s;
        carry = c;
    }
    return sums;
}

std::vector<int> Session::bv_not(const std::vector<int> &a) {
    std::vector<int> r(a.size());
    for (size_t i = 0; i < a.size(); i++) r[i] = -a[i];
    return r;
}

std::vector<int> Session::bv_neg(const std::vector<int> &a) {
    return bv_add(bv_not(a), int_to_bits(1, (int)a.size()));
}

std::vector<int> Session::bv_sub(const std::vector<int> &a, const std::vector<int> &b) {
    return bv_add(a, bv_neg(b));
}

std::vector<int> Session::bv_bitwise(Op op, const std::vector<int> &a, const std::vector<int> &b) {
    std::vector<int> r(a.size());
    for (size_t i = 0; i < a.size(); i++) {
        int x = op == OP_BVAND || op == OP_BVNAND ? gate_and(a[i], b[i])
              : op == OP_BVOR || op == OP_BVNOR ? gate_or(a[i], b[i]) : gate_xor(a[i], b[i]);
        r[i] = (op == OP_BVNAND || op == OP_BVNOR || op == OP_BVXNOR) ? -x : x;
    }
    return r;
}

std::vector<int> Session::bv_mul(const std::vector<int> &a, const std::vector<int> &b) {
    int w = (int)a.size();
    std::vector<int> result = int_to_bits(0, w);
    for (int i = w - 1; i >= 0; i--) {
        int shift = w - 1 - i;
        std::vector<int> masked(w);
        for (int k = 0; k < w; k++)
            masked[k] = gate_and(k + shift < w ? a[k + shift] : CONST_FALSE(), b[i]);
        result = bv_add(result, masked);
    }
    return result;
}

std::vector<int> Session::bv_shl_const(const std::vector<int> &a, int k) {
    int w = (int)a.size();
    std::vector<int> r(w);
    for (int i = 0; i < w; i++) r[i] = i + k < w ? a[i + k] : CONST_FALSE();
    return r;
}

std::vector<int> Session::bv_lshr_const(const std::vector<int> &a, int k) {
    int w = (int)a.size();
    std::vector<int> r(w);
    for (int i = 0; i < w; i++) r[i] = i >= k ? a[i - k] : CONST_FALSE();
    return r;
}

std::vector<int> Session::bv_urem_pow2_const(const std::vector<int> &a, int k) {
    int w = (int)a.size();
    std::vector<int> r(w);
    for (int i = 0; i < w; i++) r[i] = i >= w - k ? a[i] : CONST_FALSE();
    return r;
}

std::vector<int> Session::bv_mul_by_sparse_const(const std::vector<int> &x, uint64_t c, int w) {
    std::vector<int> acc;
    bool first = true;
    for (int i = 0; i < std::min(w, 64); i++) {
        if (!((c >> i) & 1ULL)) continue;
        std::vector<int> shifted = bv_shl_const(x, i);
        if (first) { acc = shifted; first = false; }
        else acc = bv_add(acc, shifted);
    }
    return acc;
}

void Session::bv_reldiv_by_const(const std::vector<int> &a, uint64_t c, int w,
                                 std::vector<int> &q_out, std::vector<int> &r_out) {
    int k = 0;
    for (uint64_t rem = c; rem; rem >>= 1) k++;
    int ew = w + k;
    std::vector<int> q(w), r(w);
    for (int i = 0; i < w; i++) q[i] = fresh();
    for (int i = 0; i < w; i++) r[i] = fresh();
    std::vector<int> q_ext(ew), r_ext(ew), a_ext(ew);
    for (int i = 0; i < k; i++) q_ext[i] = r_ext[i] = a_ext[i] = CONST_FALSE();
    for (int i = 0; i < w; i++) { q_ext[k + i] = q[i]; r_ext[k + i] = r[i]; a_ext[k + i] = a[i]; }
    std::vector<int> sum = bv_add(bv_mul_by_sparse_const(q_ext, c, ew), r_ext);
    for (int i = 0; i < ew; i++) {
        addClauseLits({sum[i], -a_ext[i]});
        addClauseLits({-sum[i], a_ext[i]});
    }
    addClauseLits({bv_ult(r, int_to_bits(c, w))});
    q_out = q;
    r_out = r;
}

int Session::bv_eq(const std::vector<int> &a, const std::vector<int> &b) {
    std::vector<int> eq(a.size());
    for (size_t i = 0; i < a.size(); i++) eq[i] = -gate_xor(a[i], b[i]);
    return gate_and_n(eq);
}

int Session::bv_ult(const std::vector<int> &a, const std::vector<int> &b) {
    int borrow = CONST_FALSE();
    for (int i = (int)a.size() - 1; i >= 0; i--) {
        int lt = gate_and(-a[i], b[i]);
        int eq = -gate_xor(a[i], b[i]);
        borrow = gate_or(lt, gate_and(eq, borrow));
    }
    return borrow;
}

int Session::bv_slt(const std::vector<int> &a, const std::vector<int> &b) {
    int diff_sign = gate_and(a[0], -b[0]);
    int same_sign = -gate_xor(a[0], b[0]);
    return gate_or(diff_sign, gate_and(same_sign, bv_ult(a, b)));
}

void Session::bv_udivrem(const std::vector<int> &a, const std::vector<int> &b,
                         std::vector<int> &q_out, std::vector<int> &r_out) {
    int w = (int)a.size();
    std::vector<int> r(w + 1, CONST_FALSE());
    std::vector<int> b_ext(w + 1);
    b_ext[0] = CONST_FALSE();
    for (int i = 0; i < w; i++) b_ext[i + 1] = b[i];
    std::vector<int> q;
    for (int i = 0; i < w; i++) {
        std::vector<int> shifted(w + 1);
        for (int k = 0; k < w; k++) shifted[k] = r[k + 1];
        shifted[w] = a[i];
        r = shifted;
        int ge = -bv_ult(r, b_ext);
        std::vector<int> r_sub = bv_sub(r, b_ext);
        for (int k = 0; k < w + 1; k++) r[k] = gate_ite(ge, r_sub[k], r[k]);
        q.push_back(ge);
    }
    q_out = q;
    r_out = std::vector<int>(r.begin() + 1, r.end());
}

std::vector<int> Session::bv_bvudiv(const std::vector<int> &a, const std::vector<int> &b) {
    std::vector<int> q, r;
    bv_udivrem(a, b, q, r);
    int bz = -gate_or_n(b);
    for (size_t k = 0; k < q.size(); k++) q[k] = gate_ite(bz, CONST_TRUE(), q[k]);
    return q;
}

std::vector<int> Session::bv_bvurem(const std::vector<int> &a, const std::vector<int> &b) {
    std::vector<int> q, r;
    bv_udivrem(a, b, q, r);
    int bz = -gate_or_n(b);
    for (size_t k = 0; k < r.size(); k++) r[k] = gate_ite(bz, a[k], r[k]);
    return r;
}

std::vector<int> Session::bv_bvsdiv(const std::vector<int> &a, const std::vector<int> &b) {
    int w = (int)a.size();
    std::vector<int> an = bv_neg(a), bn = bv_neg(b), am(w), bm(w);
    for (int k = 0; k < w; k++) { am[k] = gate_ite(a[0], an[k], a[k]); bm[k] = gate_ite(b[0], bn[k], b[k]); }
    std::vector<int> qm, rm;
    bv_udivrem(am, bm, qm, rm);
    std::vector<int> qn = bv_neg(qm);
    int sd = gate_xor(a[0], b[0]);
    int bz = -gate_or_n(b);
    std::vector<int> out(w);
    for (int k = 0; k < w; k++) {
        int res = gate_ite(sd, qn[k], qm[k]);
        int one = k == w - 1 ? CONST_TRUE() : CONST_FALSE();
        out[k] = gate_ite(bz, gate_ite(a[0], one, CONST_TRUE()), res);
    }
    return out;
}

std::vector<int> Session::bv_bvsrem(const std::vector<int> &a, const std::vector<int> &b) {
    int w = (int)a.size();
    std::vector<int> an = bv_neg(a), bn = bv_neg(b), am(w), bm(w);
    for (int k = 0; k < w; k++) { am[k] = gate_ite(a[0], an[k], a[k]); bm[k] = gate_ite(b[0], bn[k], b[k]); }
    std::vector<int> qm, rm;
    bv_udivrem(am, bm, qm, rm);
    std::vector<int> rn = bv_neg(rm);
    int bz = -gate_or_n(b);
    std::vector<int> out(w);
    for (int k = 0; k < w; k++) out[k] = gate_ite(bz, a[k], gate_ite(a[0], rn[k], rm[k]));
    return out;
}

std::vector<int> Session::bv_shift(Op op, const std::vector<int> &a, const std::vector<int> &b) {
    int w = (int)a.size();
    int sign = a[0];
    int fillBit = op == OP_BVASHR ? sign : CONST_FALSE();
    std::vector<int> result = a;
    int nstages = (int)b.size();
    for (int stage = 0; stage < nstages; stage++) {
        int bit = b[nstages - 1 - stage];
        int amt = stage < 31 ? int(1u << stage) : w;
        if (amt >= w) {
            // at saturation every remaining (higher) shift-amount bit matters
            for (int j = 0; j < nstages - 1 - stage; ++j) bit = gate_or(bit, b[j]);
            for (int k = 0; k < w; k++) result[k] = gate_ite(bit, fillBit, result[k]);
            break;
        }
        std::vector<int> shifted(w);
        for (int k = 0; k < w; k++) {
            if (op == OP_BVSHL) shifted[k] = k + amt < w ? result[k + amt] : CONST_FALSE();
            else shifted[k] = k >= amt ? result[k - amt] : fillBit;
        }
        for (int k = 0; k < w; k++) result[k] = gate_ite(bit, shifted[k], result[k]);
    }
    return result;
}

// A constant operand that fits in 64 bits (strength reductions only).
bool Session::smallConst(int t, uint64_t &cv) {
    Val v;
    if (terms[t].sort.isBool || terms[t].sort.width > 64 || !constValue(t, v)) return false;
    cv = 0;
    for (uint8_t b : v.v) cv = (cv << 1) | b;
    return true;
}

// ── bit-blasting (term-bit caches are frame-owned) ─────────────────────────

std::vector<int> Session::blastBV(int t) {
    auto it = bvCache.find(t);
    if (it != bvCache.end()) return it->second;
    Term n = terms[t];   // copy: blasting never adds terms, but keep it simple
    std::vector<int> out;
    int w = n.sort.width;
    if (n.op == OP_VAR) {
        // primary bits belong to the symbol, not to any frame
        auto sb = symBits.find(n.sym);
        if (sb != symBits.end()) return sb->second;
        out.resize(w);
        for (int i = 0; i < w; i++) out[i] = fresh();
        symBits[n.sym] = out;
        return out;
    }
    Val cval;
    if (n.isConst && constValue(t, cval)) {
        out = bitsOfVal(cval.v);
    } else {
        switch (n.op) {
        case OP_ITE: {
            int c = blastBool(n.args[0]);
            std::vector<int> a = blastBV(n.args[1]), b = blastBV(n.args[2]);
            out.resize(w);
            for (int i = 0; i < w; i++) out[i] = gate_ite(c, a[i], b[i]);
            break;
        }
        case OP_BVNOT: out = bv_not(blastBV(n.args[0])); break;
        case OP_BVNEG: out = bv_neg(blastBV(n.args[0])); break;
        case OP_BVAND: case OP_BVOR: case OP_BVXOR: case OP_BVNAND: case OP_BVNOR: case OP_BVXNOR:
            out = bv_bitwise(n.op, blastBV(n.args[0]), blastBV(n.args[1]));
            break;
        case OP_BVADD: out = bv_add(blastBV(n.args[0]), blastBV(n.args[1])); break;
        case OP_BVSUB: out = bv_sub(blastBV(n.args[0]), blastBV(n.args[1])); break;
        case OP_BVMUL: {
            uint64_t mask = w >= 64 ? ~0ULL : ((1ULL << w) - 1);
            bool handled = false;
            for (int side = 0; side < 2 && !handled; side++) {
                uint64_t cv;
                if (!smallConst(n.args[side], cv)) continue;
                cv &= mask;
                int k;
                std::vector<int> x = blastBV(n.args[1 - side]);
                if (cv == 0) out = int_to_bits(0, w);
                else if (cv == 1) out = x;
                else if (cv == mask) out = bv_neg(x);
                else if (isPow2Const(cv, k)) out = bv_shl_const(x, k);
                else out = bv_mul_by_sparse_const(x, cv, w);
                handled = true;
            }
            if (!handled) out = bv_mul(blastBV(n.args[0]), blastBV(n.args[1]));
            break;
        }
        case OP_BVUDIV: case OP_BVUREM: {
            uint64_t cv; int k;
            if (smallConst(n.args[1], cv) && cv != 0) {
                std::vector<int> a = blastBV(n.args[0]);
                if (isPow2Const(cv, k)) {
                    out = n.op == OP_BVUDIV ? bv_lshr_const(a, k) : bv_urem_pow2_const(a, k);
                } else {
                    std::vector<int> q, r;
                    bv_reldiv_by_const(a, cv, w, q, r);
                    out = n.op == OP_BVUDIV ? q : r;
                }
            } else {
                std::vector<int> a = blastBV(n.args[0]), b = blastBV(n.args[1]);
                out = n.op == OP_BVUDIV ? bv_bvudiv(a, b) : bv_bvurem(a, b);
            }
            break;
        }
        case OP_BVSDIV: out = bv_bvsdiv(blastBV(n.args[0]), blastBV(n.args[1])); break;
        case OP_BVSREM: out = bv_bvsrem(blastBV(n.args[0]), blastBV(n.args[1])); break;
        case OP_BVSMOD: {
            std::vector<int> a = blastBV(n.args[0]), b = blastBV(n.args[1]);
            std::vector<int> r = bv_bvsrem(a, b);
            int adjust = gate_and(gate_or_n(r), gate_xor(a[0], b[0]));
            std::vector<int> corrected = bv_add(r, b);
            out.resize(w);
            for (int i = 0; i < w; i++) out[i] = gate_ite(adjust, corrected[i], r[i]);
            break;
        }
        case OP_BVSHL: case OP_BVLSHR: case OP_BVASHR:
            out = bv_shift(n.op, blastBV(n.args[0]), blastBV(n.args[1]));
            break;
        case OP_CONCAT: {
            out = blastBV(n.args[0]);
            std::vector<int> b = blastBV(n.args[1]);
            out.insert(out.end(), b.begin(), b.end());
            break;
        }
        case OP_EXTRACT: {
            std::vector<int> a = blastBV(n.args[0]);
            int aw = (int)a.size();
            out.assign(a.begin() + (aw - 1 - n.params[0]), a.begin() + (aw - n.params[1]));
            break;
        }
        case OP_ZEXT: case OP_SEXT: {
            std::vector<int> a = blastBV(n.args[0]);
            out.assign(n.params[0], n.op == OP_ZEXT ? CONST_FALSE() : a[0]);
            out.insert(out.end(), a.begin(), a.end());
            break;
        }
        case OP_ROTL: case OP_ROTR: {
            std::vector<int> a = blastBV(n.args[0]);
            size_t aw = a.size(), k = (size_t)(n.params[0] % (long long)aw);
            out.resize(aw);
            for (size_t i = 0; i < aw; i++)
                out[i] = n.op == OP_ROTL ? a[(i + k) % aw] : a[(i + aw - k) % aw];
            break;
        }
        case OP_REPEAT: {
            std::vector<int> a = blastBV(n.args[0]);
            for (long long i = 0; i < n.params[0]; i++) out.insert(out.end(), a.begin(), a.end());
            break;
        }
        case OP_BVCOMP: out = {bv_eq(blastBV(n.args[0]), blastBV(n.args[1]))}; break;
        default:
            throw SessionError("internal error: not a bit-vector term");
        }
    }
    bvCache[t] = out;
    frames.back().bvTerms.push_back(t);
    return out;
}

int Session::blastBool(int t) {
    auto it = boolCache.find(t);
    if (it != boolCache.end()) return it->second;
    Term n = terms[t];
    int out;
    if (n.op == OP_VAR) {
        auto sb = symBits.find(n.sym);
        if (sb != symBits.end()) return sb->second[0];
        out = fresh();
        symBits[n.sym] = {out};
        return out;
    }
    Val cval;
    if (n.isConst && constValue(t, cval)) {
        out = cval.b ? CONST_TRUE() : CONST_FALSE();
    } else {
        switch (n.op) {
        case OP_NOT: out = -blastBool(n.args[0]); break;
        case OP_AND: case OP_OR: {
            std::vector<int> bits;
            for (int a : n.args) bits.push_back(blastBool(a));
            out = n.op == OP_AND ? gate_and_n(bits) : gate_or_n(bits);
            break;
        }
        case OP_XOR: out = gate_xor(blastBool(n.args[0]), blastBool(n.args[1])); break;
        case OP_IMPLIES: out = gate_or(-blastBool(n.args[0]), blastBool(n.args[1])); break;
        case OP_ITE: out = gate_ite(blastBool(n.args[0]), blastBool(n.args[1]), blastBool(n.args[2])); break;
        case OP_EQ:
            if (terms[n.args[0]].sort.isBool) out = -gate_xor(blastBool(n.args[0]), blastBool(n.args[1]));
            else out = bv_eq(blastBV(n.args[0]), blastBV(n.args[1]));
            break;
        case OP_DISTINCT: {
            bool isB = terms[n.args[0]].sort.isBool;
            std::vector<int> bs;
            std::vector<std::vector<int>> vs;
            for (int a : n.args) {
                if (isB) bs.push_back(blastBool(a));
                else vs.push_back(blastBV(a));
            }
            std::vector<int> neqs;
            for (size_t i = 0; i < n.args.size(); i++)
                for (size_t j = i + 1; j < n.args.size(); j++)
                    neqs.push_back(isB ? gate_xor(bs[i], bs[j]) : -bv_eq(vs[i], vs[j]));
            out = gate_and_n(neqs);
            break;
        }
        case OP_ULT: out = bv_ult(blastBV(n.args[0]), blastBV(n.args[1])); break;
        case OP_ULE: out = bv_ule(blastBV(n.args[0]), blastBV(n.args[1])); break;
        case OP_UGT: out = bv_ult(blastBV(n.args[1]), blastBV(n.args[0])); break;
        case OP_UGE: out = bv_ule(blastBV(n.args[1]), blastBV(n.args[0])); break;
        case OP_SLT: out = bv_slt(blastBV(n.args[0]), blastBV(n.args[1])); break;
        case OP_SLE: out = bv_sle(blastBV(n.args[0]), blastBV(n.args[1])); break;
        case OP_SGT: out = bv_slt(blastBV(n.args[1]), blastBV(n.args[0])); break;
        case OP_SGE: out = bv_sle(blastBV(n.args[1]), blastBV(n.args[0])); break;
        default:
            throw SessionError("internal error: not a Bool term");
        }
    }
    boolCache[t] = out;
    frames.back().boolTerms.push_back(t);
    return out;
}

// ── commands ───────────────────────────────────────────────────────────────

void Session::cmdDeclare(const std::string &name, const Sort &s) {
    int uid = (int)symSorts.size();
    Term t;
    t.op = OP_VAR;
    t.sort = s;
    t.sym = uid;
    bindName(name, Binding{false, -1});   // throws before any state change on clash
    symSorts.push_back(s);
    symNames.push_back(name);
    int id = mk(t);
    bindings[name].back().id = id;
    declOrder.push_back(uid);
}

void Session::cmdDefine(const SExpr &cmd) {
    // (define-fun f ((x S) ...) R body)
    if (cmd.kids.size() != 5 || cmd.kids[1].kind != SExpr::ATOM || cmd.kids[2].kind != SExpr::LIST)
        throw SessionError("malformed define-fun");
    const std::string &name = cmd.kids[1].text;
    if (bindings.count(name) && !bindings[name].empty())
        throw SessionError("symbol " + name + " already declared");
    std::unordered_map<std::string,int> scope;
    int idx = 0;
    for (const SExpr &p : cmd.kids[2].kids) {
        if (p.kind != SExpr::LIST || p.kids.size() != 2 || p.kids[0].kind != SExpr::ATOM)
            throw SessionError("malformed parameter list");
        Term pt;
        pt.op = OP_PARAM;
        pt.sort = parseSort(p.kids[1]);
        pt.sym = idx++;
        scope[p.kids[0].text] = mk(pt);
    }
    Sort result = parseSort(cmd.kids[3]);
    std::vector<std::unordered_map<std::string,int>> env{scope};
    int body = build(cmd.kids[4], env);
    if (terms[body].sort != result) throw SessionError("define-fun body does not have the declared sort");
    Macro m;
    m.nparams = idx;
    m.body = body;
    m.result = result;
    macros.push_back(m);
    bindName(name, Binding{true, (int)macros.size() - 1});
}

void Session::cmdAssert(const SExpr &cmd) {
    if (cmd.kids.size() != 2) throw SessionError("malformed assert");
    int t = buildTop(cmd.kids[1]);            // validates fully before any SAT change
    if (!terms[t].sort.isBool) throw SessionError("assert expects a Bool term");
    try {
        int lit = blastBool(t);
        addClauseLits({lit});
    } catch (...) {
        poisoned = true;                      // SAT state may be half-updated
        throw;
    }
}

void Session::cmdPush(long long n) {
    if (n < 0) throw SessionError("push expects a non-negative numeral");
    for (long long i = 0; i < n; i++) {
        Frame f;
        f.uid = nextFrameUid++;
        f.sel = fresh();                      // never asserted; used as an assumption
        frames.push_back(f);
    }
}

void Session::cmdPop(long long n) {
    if (n < 0) throw SessionError("pop expects a non-negative numeral");
    if (n > (long long)frames.size() - 1) throw SessionError("pop: not enough pushed levels");
    for (long long i = 0; i < n; i++) {
        Frame &f = frames.back();
        // Permanently disable every clause this frame created.
        vec<Lit> unit;
        unit.push(toLit(-f.sel));
        S->addClause(unit);
        for (auto &k : f.andKeys) andCache.erase(k);
        for (auto &k : f.xorKeys) xorCache.erase(k);
        for (int t : f.bvTerms) bvCache.erase(t);
        for (int t : f.boolTerms) boolCache.erase(t);
        for (auto it = f.names.rbegin(); it != f.names.rend(); ++it) {
            auto &stack = bindings[*it];
            if (!stack.empty()) stack.pop_back();
            if (stack.empty()) bindings.erase(*it);
        }
        frames.pop_back();
    }
}

void Session::cmdCheck(const std::vector<int> &assumptionLits) {
    invalidateModel();
    if (!S->okay()) { out << "unsat\n"; out.flush(); return; }
    vec<Lit> assumps;
    for (size_t i = 1; i < frames.size(); i++) assumps.push(toLit(frames[i].sel));
    for (int l : assumptionLits) assumps.push(toLit(l));
    lbool r = S->solveLimited(assumps);
    if (r == l_True) {
        haveModel = true;
        model.resize(S->model.size());
        for (int i = 0; i < S->model.size(); i++) model[i] = S->model[i];
        out << "sat\n";
    } else if (r == l_False) {
        out << "unsat\n";
    } else {
        lastUnknownReason = "incomplete";
        out << "unknown\n";
    }
    out.flush();
}

std::string Session::showVal(const Val &v, const Sort &s) {
    if (s.isBool) return v.b ? "true" : "false";
    std::string r = "#b";
    for (uint8_t b : v.v) r += b ? '1' : '0';
    return r;
}

void Session::cmdGetValue(const SExpr &cmd) {
    if (cmd.kids.size() != 2 || cmd.kids[1].kind != SExpr::LIST || cmd.kids[1].kids.empty())
        throw SessionError("malformed get-value");
    if (!produceModels) throw SessionError("model production is not enabled");
    if (!haveModel) throw SessionError("no model available: the last check was not sat");
    std::vector<int> ts;
    for (const SExpr &e : cmd.kids[1].kids) ts.push_back(buildTop(e));   // no blasting
    std::unordered_map<int, Val> memo;
    std::string r = "(";
    for (size_t i = 0; i < ts.size(); i++) {
        if (i) r += "\n ";
        r += "(" + show(cmd.kids[1].kids[i]) + " " + showVal(eval(ts[i], memo, true), terms[ts[i]].sort) + ")";
    }
    out << r << ")\n";
    out.flush();
}

void Session::cmdGetModel() {
    if (!produceModels) throw SessionError("model production is not enabled");
    if (!haveModel) throw SessionError("no model available: the last check was not sat");
    std::unordered_map<int, Val> memo;
    std::string r = "(";
    for (int uid : declOrder) {
        auto bit = bindings.find(symNames[uid]);
        if (bit == bindings.end() || bit->second.empty() || bit->second.back().isMacro) continue;
        int t = bit->second.back().id;
        if (terms[t].sym != uid) continue;      // name now bound to a newer declaration
        r += "\n  (define-fun " + symNames[uid] + " () " + showSort(symSorts[uid]) + " " +
             showVal(eval(t, memo, true), symSorts[uid]) + ")";
    }
    out << r << "\n)\n";
    out.flush();
}

static long long numeralArg(const SExpr &cmd, long long dflt) {
    if (cmd.kids.size() == 1) return dflt;
    if (cmd.kids.size() != 2 || cmd.kids[1].kind != SExpr::ATOM || !isNumeral(cmd.kids[1].text))
        throw SessionError("expected a numeral");
    return std::stoll(cmd.kids[1].text);
}

bool Session::command(const SExpr &cmd) {
    if (cmd.kind != SExpr::LIST || cmd.kids.empty() || cmd.kids[0].kind != SExpr::ATOM) {
        error("malformed command");
        return true;
    }
    const std::string &c = cmd.kids[0].text;
    if (c == "exit") { success(); return false; }
    if (poisoned && c != "reset") {
        error("session is in an inconsistent state after an internal failure; reset required");
        return true;
    }
    try {
        if (c == "set-logic") {
            if (cmd.kids.size() != 2) throw SessionError("malformed set-logic");
            if (logicSet) throw SessionError("logic already set");
            const std::string &l = cmd.kids[1].text;
            if (l != "QF_BV" && l != "QF_UFBV" && l != "ALL") { unsupported(); return true; }
            logicSet = true;
            success();
        } else if (c == "set-option") {
            if (cmd.kids.size() != 3) throw SessionError("malformed set-option");
            const std::string &k = cmd.kids[1].text;
            bool on = cmd.kids[2].isAtom("true");
            if (k == ":print-success") printSuccess = on;
            else if (k == ":produce-models") produceModels = on;
            else if (k == ":global-declarations") {
                if (!declOrder.empty() || frames.size() > 1)
                    throw SessionError(":global-declarations must be set before declarations and push");
                globalDecls = on;
            } else if (k == ":diagnostic-output-channel" || k == ":regular-output-channel" ||
                       k == ":random-seed" || k == ":verbosity" || k == ":produce-unsat-cores" ||
                       k == ":produce-assignments" || k == ":interactive-mode" || k == ":produce-assertions") {
                // accepted, no effect on answers
            } else { unsupported(); return true; }
            success();
        } else if (c == "set-info") {
            success();
        } else if (c == "get-info") {
            if (cmd.kids.size() != 2) throw SessionError("malformed get-info");
            const std::string &k = cmd.kids[1].text;
            if (k == ":name") out << "(:name \"NeuroSym\")\n";
            else if (k == ":version") out << "(:version \"interactive-v1\")\n";
            else if (k == ":authors") out << "(:authors \"NeuroSym\")\n";
            else if (k == ":error-behavior") out << "(:error-behavior continued-execution)\n";
            else if (k == ":reason-unknown") out << "(:reason-unknown " << lastUnknownReason << ")\n";
            else { unsupported(); return true; }
            out.flush();
        } else if (c == "declare-fun") {
            if (cmd.kids.size() != 4 || cmd.kids[1].kind != SExpr::ATOM || cmd.kids[2].kind != SExpr::LIST)
                throw SessionError("malformed declare-fun");
            if (!cmd.kids[2].kids.empty()) throw SessionError("uninterpreted functions are not supported");
            invalidateModel();
            cmdDeclare(cmd.kids[1].text, parseSort(cmd.kids[3]));
            success();
        } else if (c == "declare-const") {
            if (cmd.kids.size() != 3 || cmd.kids[1].kind != SExpr::ATOM) throw SessionError("malformed declare-const");
            invalidateModel();
            cmdDeclare(cmd.kids[1].text, parseSort(cmd.kids[2]));
            success();
        } else if (c == "define-fun") {
            invalidateModel();
            cmdDefine(cmd);
            success();
        } else if (c == "assert") {
            invalidateModel();
            cmdAssert(cmd);
            success();
        } else if (c == "push") {
            invalidateModel();
            cmdPush(numeralArg(cmd, 1));
            success();
        } else if (c == "pop") {
            invalidateModel();
            cmdPop(numeralArg(cmd, 1));
            success();
        } else if (c == "check-sat") {
            if (cmd.kids.size() != 1) throw SessionError("malformed check-sat");
            cmdCheck({});
        } else if (c == "check-sat-assuming") {
            if (cmd.kids.size() != 2 || cmd.kids[1].kind != SExpr::LIST)
                throw SessionError("malformed check-sat-assuming");
            std::vector<int> lits;
            std::vector<bool> negs;
            for (const SExpr &a : cmd.kids[1].kids) {
                bool negated = false;
                const SExpr *sym = &a;
                if (a.kind == SExpr::LIST && a.kids.size() == 2 && a.kids[0].isAtom("not")) {
                    negated = true;
                    sym = &a.kids[1];
                }
                if (sym->kind != SExpr::ATOM) throw SessionError("check-sat-assuming expects Boolean literals");
                int t = buildTop(*sym);
                if (!terms[t].sort.isBool) throw SessionError("check-sat-assuming expects Boolean literals");
                if (terms[t].op != OP_VAR && terms[t].op != OP_BOOLLIT)
                    throw SessionError("check-sat-assuming expects declared Boolean constants");
                lits.push_back(t);
                negs.push_back(negated);
            }
            std::vector<int> sat;
            try {
                for (size_t i = 0; i < lits.size(); i++) {
                    int l = blastBool(lits[i]);   // primary bit or constant: no clauses
                    sat.push_back(negs[i] ? -l : l);
                }
            } catch (...) { poisoned = true; throw; }
            cmdCheck(sat);
        } else if (c == "get-value") {
            cmdGetValue(cmd);
        } else if (c == "get-model") {
            cmdGetModel();
        } else if (c == "reset") {
            reset();
            success();
        } else if (c == "echo") {
            if (cmd.kids.size() != 2 || cmd.kids[1].kind != SExpr::STRING) throw SessionError("malformed echo");
            out << show(cmd.kids[1]) << "\n";
            out.flush();
        } else if (c == "reset-assertions" || c == "get-assertions" || c == "get-unsat-core" ||
                   c == "get-assignment" || c == "get-proof" || c == "get-unsat-assumptions" ||
                   c == "declare-sort" || c == "define-sort" || c == "define-fun-rec" ||
                   c == "define-funs-rec" || c == "declare-datatype" || c == "declare-datatypes") {
            unsupported();
        } else {
            throw SessionError("unknown command " + c);
        }
    } catch (const SessionError &e) {
        error(e.what());
    } catch (const ParseError &e) {
        error(e.what());
    } catch (const Minisat::OutOfMemoryException &) {
        poisoned = true;
        error("out of memory");
    } catch (const std::bad_alloc &) {
        poisoned = true;
        error("out of memory");
    } catch (const std::exception &e) {
        error(e.what());
    }
    return true;
}

// ── driver ─────────────────────────────────────────────────────────────────

static int runLoop() {
    std::ios::sync_with_stdio(false);
    Session session(std::cout);
    Reader reader(std::cin);
    for (;;) {
        SExpr cmd;
        try {
            if (!reader.read(cmd)) return 0;
        } catch (const ParseError &e) {
            std::cout << "(error \"" << e.what() << "\")\n";
            std::cout.flush();
            return 1;
        }
        if (!session.command(cmd)) return 0;
    }
}

static void *threadMain(void *arg) {
    *(int *)arg = runLoop();
    return nullptr;
}

// Deep let-nesting and deep terms are handled recursively, so run the loop on
// a thread with a large stack instead of the default 8 MB.
static int run_interactive() {
    pthread_attr_t attr;
    pthread_attr_init(&attr);
    pthread_attr_setstacksize(&attr, (size_t)1 << 30);
    pthread_t th;
    int rc = 0;
    if (pthread_create(&th, &attr, threadMain, &rc) != 0) return runLoop();
    pthread_join(th, nullptr);
    pthread_attr_destroy(&attr);
    return rc;
}

} // namespace nsi
