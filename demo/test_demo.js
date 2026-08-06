// test_demo.js — headless check that the browser demo is actually playable.
// GPLv3.   Usage:  node demo/test_demo.js
//
// Loads demo/index.html, stubs just enough DOM for the inline script to run,
// then plays the three missions the way a learner would and asserts that each
// engine predicate fires. Catches the things that matter: path resolution,
// case sensitivity, `cd ..` / `cd -`, and mission 3's two-command history rule.

const fs = require("fs"), path = require("path"), vm = require("vm");

const html = fs.readFileSync(path.join(__dirname, "index.html"), "utf8");
const src = html.match(/<script>([\s\S]*?)<\/script>/)[1];

const out = [];
function el() {
  const o = { className: "", children: [], style: {},
              set textContent(v) { o._t = v; }, get textContent() { return o._t || ""; },
              setAttribute() {}, appendChild(c) { o.children.push(c); },
              addEventListener() {}, focus() {}, set innerHTML(v) { o._h = v; o.children = []; },
              get innerHTML() { return o._h || ""; } };
  return o;
}
const nodes = {};
const doc = {
  documentElement: {},
  getElementById(id) { return nodes[id] || (nodes[id] = el()); },
  createElement() { return el(); },
  createTextNode(t) { return { textContent: t }; },
  addEventListener() {},
};
const ctx = { document: doc, setInterval: () => 0, clearInterval: () => {},
              setTimeout: (f) => { f(); return 0; }, console };
ctx.globalThis = ctx;
vm.createContext(ctx);
vm.runInContext(src + "\n;globalThis.__p={run,flush,boot,line," +
  "pwd:()=>pwdStr(cwd),mi:()=>mi,fin:()=>finished,lang:v=>{lang=v;boot();}," +
  "lvl:()=>level,q:()=>queue};", ctx);
const P = ctx.__p;

// capture what the terminal prints
const printed = [];
const realLine = P.line;
ctx.line = function (t, c) { printed.push([c || "", t]); return realLine(t, c); };

let fails = 0;
function check(name, cond) {
  console.log((cond ? "  ok   " : "  FAIL ") + name);
  if (!cond) fails++;
}
function type(cmd) { printed.length = 0; P.run(cmd); P.flush(); }
function said() { return P.q().map(x => x.t).join("\n") + printed.map(p => p[1]).join("\n"); }

for (const lang of ["fr", "en"]) {
  const FR = lang === "fr";
  console.log("\n== " + lang.toUpperCase() + " ==");
  P.lang(lang);
  const C = FR ? "Chateau" : "Castle",
        TOWER = FR ? "Donjon" : "Main_tower",
        F1 = FR ? "Premier_etage" : "First_floor",
        F2 = FR ? "Deuxieme_etage" : "Second_floor",
        TOP = FR ? "Haut_du_donjon" : "Top_of_the_tower",
        CELLAR = FR ? "Cave" : "Cellar",
        MAIN = FR ? "Batiment_principal" : "Main_building",
        THRONE = FR ? "Salle_du_trone" : "Throne_room";

  check("starts at home, mission 1", P.mi() === 0 && P.pwd() === "~");
  type("ls");
  check("ls lists the castle", printed.some(p => p[1] && p[1].includes(C)));

  type("cd " + C.toLowerCase());
  check("case sensitive: lowercase fails",
        printed.some(p => p[0] === "err"));
  check("a wrong path raises the hint level", P.lvl() >= 1);

  type("cd nowhere");
  check("unknown dir gives a shell error + tutor reply",
        printed.some(p => p[0] === "err"));

  type("badcmd");
  check("unknown command reported",
        printed.some(p => p[0] === "err" && /not found|introuvable/.test(p[1])));

  // mission 1: walk to the top of the tower
  type(`cd ${C}/${TOWER}/${F1}/${F2}/${TOP}`);
  check("mission 1 passes on arrival", P.mi() === 1);
  check("mission 2 starts at the top of the tower",
        P.pwd().endsWith(TOP));

  // mission 2: cd .. down to the cellar.  Top_of_the_tower sits five levels
  // deep (Castle/Main_tower/First_floor/Second_floor/Top_of_the_tower), so it
  // takes four hops to get back to the castle courtyard.
  for (let i = 0; i < 4; i++) type("cd ..");
  check("cd .. walks back up", P.pwd() === "~/" + C);
  type("cd " + CELLAR);
  check("mission 2 passes in the cellar", P.mi() === 2);
  check("mission 3 starts in the cellar", P.pwd() === "~/" + C + "/" + CELLAR);

  // mission 3: must be exactly `cd` then a direct jump
  type(`cd ${MAIN}`);
  check("mission 3 not passed by wandering", P.mi() === 2);
  type(`cd ${THRONE}`);
  check("arriving without `cd` first does NOT pass", P.mi() === 2 && !P.fin());
  type("cd");
  check("`cd` returns home", P.pwd() === "~");
  type(`cd ${C}/${MAIN}/${THRONE}`);
  check("mission 3 passes with cd + direct jump", P.fin() === true);
}

console.log("\n== extras ==");
P.lang("fr");
type("cd Chateau");
type("cd -");
check("cd - jumps back", P.pwd() === "~");
type("pwd");
check("pwd prints the path", printed.some(p => p[1] === "~"));
P.run("gm indice"); P.flush();
check("gm indice serves a curated hint", said().length > 40);
P.run("gm mission"); P.flush();
check("gm mission re-narrates", said().length > 80);

console.log(fails ? "\n" + fails + " FAILURE(S)" : "\nALL DEMO TESTS PASSED");
process.exit(fails ? 1 : 0);
