/* Qwendom core JS — shared behavior layer for all five screens.
 *
 * PORTING NOTE FOR CODING AGENTS:
 * Each section below is a self-contained "component" that takes data via
 * DOM `data-*` attributes (its props) and renders/enhances markup. That maps
 * directly onto a functional React component: data-* attributes == props,
 * the render function's body == the component body, the DOM writes it makes
 * == the JSX it would return. Two components (PhaseStrip, ArcTimeline) take
 * their data from a plain object under `Qwendom.data` instead of data-*
 * attributes, because that data is structured/nested rather than scalar —
 * in React those objects are exactly the `data`/`config` prop you'd pass in.
 *
 * The markup-only components (typed turn card, living-brief blame line,
 * ownership token, artifact passport, and the five review-artifact
 * renderers) have no JS here — they are static per-screen HTML because they
 * are effectively already "pre-rendered" (this prototype hand-authors the
 * markup a real app would generate from data). Their prop schemas and CSS
 * hooks are documented in /data/q4/PORTING.md so a coding agent can turn
 * each into a real React component backed by real data.
 *
 * Every init function below is idempotent and independent — a screen that
 * doesn't have the relevant markup (e.g. no #arcline) is a no-op for that
 * component, so this one file can keep being shared as-is, or be split
 * one-component-per-file later with no behavior change.
 */
(function(){
  "use strict";

  var Qwendom = (window.Qwendom = window.Qwendom || {});
  Qwendom.data = Qwendom.data || {};

  /* =========================================================================
   * Component: Nav — marks the current screen as aria-current in the rail
   * and bottom nav.
   * Props: document.body.dataset.page (string: "intake"|"live"|"review"|"recap"|"dossier")
   *        every [data-nav] anchor whose data-nav matches `page`.
   * React shape: <Nav current="intake" items={[{key,label,href}]} />
   * ========================================================================= */
  function initNav(){
    var page = document.body.dataset.page;
    document.querySelectorAll("[data-nav]").forEach(function(a){
      if(a.dataset.nav === page) a.setAttribute("aria-current","page");
    });
  }

  /* =========================================================================
   * Component: Explainer — attaches an ⓘ button + popover to any element
   * carrying data-explain-title / data-explain-text.
   * Props per instance: data-explain-title (string), data-explain-text (string)
   * React shape: <Explainer title="..." text="...">{children}</Explainer>
   * ========================================================================= */
  var explained = [];
  function initExplainers(){
    explained = Array.prototype.slice.call(document.querySelectorAll("[data-explain-title]"));
    explained.forEach(function(el){
      el.classList.add("has-exp");
      var btn = document.createElement("button");
      btn.className = "exp-btn";
      btn.setAttribute("aria-label","What am I looking at: " + el.dataset.explainTitle);
      btn.textContent = "\u24D8";
      var pop = document.createElement("div");
      pop.className = "exp-pop";
      var b = document.createElement("b"); b.textContent = el.dataset.explainTitle;
      var p = document.createElement("span"); p.textContent = el.dataset.explainText || "";
      pop.appendChild(b); pop.appendChild(p);
      btn.addEventListener("click", function(ev){
        ev.stopPropagation();
        var wasOpen = el.classList.contains("exp-open");
        closeAllPops();
        if(!wasOpen) el.classList.add("exp-open");
      });
      el.appendChild(btn); el.appendChild(pop);
    });
    document.addEventListener("click", closeAllPops);
    document.addEventListener("keydown", function(e){ if(e.key === "Escape") closeAllPops(); });
  }
  function closeAllPops(){
    explained.forEach(function(e){ e.classList.remove("exp-open","tour-focus"); });
  }

  /* =========================================================================
   * Component: Tour — steps the #tour button through every Explainer in
   * document order.
   * Props: depends on Explainer having already registered `explained`.
   * React shape: <Tour steps={explainerRefs} /> built from the same ordered
   * list a parent screen assembles from its rendered <Explainer> children.
   * ========================================================================= */
  function initTour(){
    var tourBtn = document.getElementById("tour");
    var tourIdx = -1;
    if(tourBtn && explained.length){
      tourBtn.addEventListener("click", function(ev){
        ev.stopPropagation();
        closeAllPops();
        tourIdx += 1;
        if(tourIdx >= explained.length){
          tourIdx = -1;
          tourBtn.innerHTML = "\u25B8&nbsp;&nbsp;TOUR";
          return;
        }
        var el = explained[tourIdx];
        el.classList.add("exp-open","tour-focus");
        el.scrollIntoView({block:"center"});
        tourBtn.innerHTML = "\u25B8&nbsp;&nbsp;NEXT " + (tourIdx+1) + "/" + explained.length;
      });
    }
  }

  /* =========================================================================
   * Component: StanceDial — arc = stance strength, tick = last-change angle.
   * Props per instance (read from data-* on .dial-ring):
   *   data-stance    "aligned"|"dissent"|"blocking"|"changed"  (stroke color)
   *   data-strength  0..1  (arc length as a fraction of the full circle)
   *   data-tick      0..1, or -1 for "no tick"  (angle of the last-change mark)
   * React shape: <StanceDial stance="aligned" strength={0.72} tick={0.4} />
   * (tick={null} to omit the mark instead of the -1 sentinel)
   * ========================================================================= */
  var STANCE_COLORS = {aligned:"#4E9A6F", dissent:"#C07C33", blocking:"#CE5F4E", changed:"#64809A"};
  Qwendom.data.stanceColors = STANCE_COLORS;

  function renderStanceDial(ring){
    var strength = parseFloat(ring.dataset.strength || "0.5");
    var tick = parseFloat(ring.dataset.tick || "-1");
    var color = STANCE_COLORS[ring.dataset.stance] || "#B4633A";
    var NS = "http://www.w3.org/2000/svg";
    var svg = document.createElementNS(NS,"svg");
    svg.setAttribute("viewBox","0 0 48 48");
    var track = document.createElementNS(NS,"circle");
    track.setAttribute("cx",24); track.setAttribute("cy",24); track.setAttribute("r",21);
    track.setAttribute("fill","none"); track.setAttribute("stroke","#EFE6D6"); track.setAttribute("stroke-width",3);
    svg.appendChild(track);
    var C = 2*Math.PI*21;
    var arc = document.createElementNS(NS,"circle");
    arc.setAttribute("cx",24); arc.setAttribute("cy",24); arc.setAttribute("r",21);
    arc.setAttribute("fill","none"); arc.setAttribute("stroke",color); arc.setAttribute("stroke-width",3);
    arc.setAttribute("stroke-linecap","round");
    arc.setAttribute("stroke-dasharray", (C*strength) + " " + C);
    arc.setAttribute("transform","rotate(-90 24 24)");
    svg.appendChild(arc);
    if(tick >= 0){
      var ang = (tick*360 - 90) * Math.PI/180;
      var t = document.createElementNS(NS,"line");
      t.setAttribute("x1", 24 + Math.cos(ang)*16.5); t.setAttribute("y1", 24 + Math.sin(ang)*16.5);
      t.setAttribute("x2", 24 + Math.cos(ang)*25.5); t.setAttribute("y2", 24 + Math.sin(ang)*25.5);
      t.setAttribute("stroke","#2B2419"); t.setAttribute("stroke-width",2); t.setAttribute("stroke-linecap","round");
      svg.appendChild(t);
    }
    ring.insertBefore(svg, ring.firstChild);
  }

  function initStanceDials(){
    document.querySelectorAll(".dial-ring").forEach(renderStanceDial);
  }

  /* =========================================================================
   * Component: PhaseStrip — gate-aware progress strip with keyhole gate dots.
   * Data shape (Qwendom.data.phaseStrip is exactly the `config` prop a React
   * <PhaseStrip config={...} /> would receive):
   *   total     number of ticks in the run
   *   current   index of the current tick (0-based)
   *   gates     map<tickIndex, "passed"|"open">  — readiness/decision gates
   * The completed-tick color ramp (from parchment to copper-deep) is a fixed
   * design constant, not per-run data, so it stays as a local `from`/`to`
   * pair rather than being added to the config — lift it into the
   * component's own defaultProps if you need to theme it later.
   * React shape: <PhaseStrip config={{total:48, current:33, gates:{12:"passed",44:"open"}}} />
   * ========================================================================= */
  Qwendom.data.phaseStrip = { total: 48, current: 33, gates: {12:"passed", 44:"open"} };

  function initPhaseStrip(){
    var wrap = document.getElementById("phase-dots");
    if(!wrap) return;
    var badge = document.getElementById("phase-badge");
    var cfg = Qwendom.data.phaseStrip;
    var TOTAL = cfg.total, CURRENT = cfg.current, GATES = cfg.gates;
    var from = [242,224,198], to = [122,62,34];
    for(var i=0;i<TOTAL;i++){
      var d = document.createElement("i");
      if(GATES[i]){ d.className = "gate " + GATES[i]; }
      if(i <= CURRENT && !GATES[i]){
        var t = i/CURRENT;
        var c = from.map(function(f,k){ return Math.round(f+(to[k]-f)*t); });
        d.style.background = "rgb("+c[0]+","+c[1]+","+c[2]+")";
        if(i === CURRENT) d.classList.add("cur");
      }
      wrap.appendChild(d);
    }
    requestAnimationFrame(function(){
      var target = wrap.children[CURRENT];
      if(badge && target) badge.style.left = (target.offsetLeft + target.offsetWidth/2) + "px";
    });
  }

  /* =========================================================================
   * Component: ArcTimeline — influence arcs drawn over the run's time axis,
   * unified with the alignment-trend dot chart below it. Used on Recap.
   * Qwendom.data.arcTimeline below is exactly the `data` payload a React
   * <ArcTimeline data={...} /> component would receive as its single prop:
   *   width, axisY, trendBaseY, minMinute, maxMinute, minX, maxX  (layout)
   *   officeColors      map<officeKey, hexColor>
   *   arcs              [fromMin, toMin, officeKey, label, peakY, labelAtMin][]
   *   events            [min, officeKey|null, topLabel, bottomLabel, isGate, labelLevel][]
   *   trend             [minute, alignedOfficeCount][]
   *   trendLabel        string caption drawn above the trend chart
   *   trendSplitIndex   index into `trend` where the top dot's color shifts
   *                     from copper to copper-deep (marks the decision gate)
   * React shape: <ArcTimeline data={QwendomData.recap.arcTimeline} />
   * ========================================================================= */
  Qwendom.data.arcTimeline = {
    width: 920, axisY: 150, trendBaseY: 318,
    minX: 46, maxX: 894, minMinute: 0, maxMinute: 64,
    officeColors: {st:"#B4633A", bl:"#4E9A6F", cr:"#C07C33", rk:"#CE5F4E", ar:"#64809A"},
    arcs: [
      [45, 60, "rk", "block on B upheld \u2192 decision", 40, 52.5],
      [47, 60, "ar", "precedent L-081 \u2192 vote flip", 78, 53.5],
      [42, 52, "cr", "signal critique \u2192 cohort 1 widened", 112, 47]
    ],
    events: [
      [0, null, "convened", "13:41", false, 0],
      [17, null, "scope narrowed", "13:58", false, 1],
      [21, null, "readiness gate", "14:02", true, 0],
      [23, "st", "leader elected", "14:04", false, 1],
      [38, "bl", "proposal A rev 4", "14:19", false, 0],
      [42, "cr", "dissent", "14:23", false, 1],
      [45, "rk", "block on B", "14:26", false, 0],
      [47, "ar", "mind changed", "14:28", false, 1],
      [52, "bl", "cohort 1 widened", "14:33", false, 0],
      [60, null, "decided 4\u20131", "14:41", true, 1]
    ],
    trend: [[0,3],[5,3],[10,2],[14,2],[17,3],[21,3],[25,3],[29,4],[33,3],[38,3],[42,4],[47,4],[54,5],[64,5]],
    trendLabel: "ALIGNMENT \u00B7 3/5 \u2192 5/5",
    trendSplitIndex: 12
  };

  function initArcTimeline(){
    var arcEl = document.getElementById("arcline");
    if(!arcEl) return;
    var cfg = Qwendom.data.arcTimeline;
    var NS2 = "http://www.w3.org/2000/svg";
    var W = cfg.width, AXIS = cfg.axisY, TREND_BASE = cfg.trendBaseY;
    var X0 = cfg.minX, X1 = cfg.maxX, M0 = cfg.minMinute, M1 = cfg.maxMinute;
    var x = function(m){ return X0 + (m-M0)/(M1-M0)*(X1-X0); };
    var OFFICE = cfg.officeColors;
    var svg = document.createElementNS(NS2,"svg");
    svg.setAttribute("viewBox","0 0 "+W+" 352");
    svg.setAttribute("role","img");
    svg.setAttribute("aria-label","Influence arcs over the run timeline, with alignment trend below");
    function el(name, attrs, text){
      var e = document.createElementNS(NS2,name);
      for(var k in attrs) e.setAttribute(k, attrs[k]);
      if(text) e.textContent = text;
      return e;
    }
    svg.appendChild(el("line",{x1:X0-10,y1:AXIS,x2:X1+10,y2:AXIS,stroke:"#E4D9C6","stroke-width":1.5}));
    var HALO = {stroke:"#FBF7EF","stroke-width":4,"paint-order":"stroke","stroke-linejoin":"round"};
    function textEl(attrs, str){
      for(var k in HALO) attrs[k] = HALO[k];
      return el("text", attrs, str);
    }
    cfg.arcs.forEach(function(a){
      var xa = x(a[0]), xb = x(a[1]), peak = a[4];
      var col = OFFICE[a[2]];
      svg.appendChild(el("path",{d:"M"+xa+","+(AXIS-6)+" Q"+((xa+xb)/2)+","+peak+" "+xb+","+(AXIS-6),fill:"none",stroke:col,"stroke-width":1.8}));
      svg.appendChild(el("path",{d:"M"+(xb-5)+","+(AXIS-14)+" L"+xb+","+(AXIS-5)+" L"+(xb-9)+","+(AXIS-9)+" Z",fill:col}));
      svg.appendChild(textEl({x:x(a[5]),y:peak-6,"text-anchor":"middle","font-size":10.5,"font-family":"Menlo,monospace",fill:col}, a[3]));
    });
    cfg.events.forEach(function(ev){
      var ex = x(ev[0]);
      if(ev[4]){
        svg.appendChild(el("circle",{cx:ex,cy:AXIS,r:8,fill:"#FFFEFA",stroke:"#B4633A","stroke-width":2}));
        svg.appendChild(el("circle",{cx:ex,cy:AXIS,r:3.5,fill:"#B4633A"}));
      } else {
        svg.appendChild(el("circle",{cx:ex,cy:AXIS,r:5,fill:ev[1]?OFFICE[ev[1]]:"#C9B89E"}));
      }
      var ly = ev[5] === 0 ? AXIS+27 : AXIS+55;
      svg.appendChild(el("line",{x1:ex,y1:AXIS+10,x2:ex,y2:ly-10,stroke:"#E4D9C6","stroke-width":1}));
      svg.appendChild(textEl({x:ex,y:ly,"text-anchor":"middle","font-size":10,"font-family":"-apple-system,sans-serif",fill:"#7C7160"}, ev[2]));
      svg.appendChild(textEl({x:ex,y:ly+12,"text-anchor":"middle","font-size":9,"font-family":"Menlo,monospace",fill:"#B8AC99"}, ev[3]));
    });
    svg.appendChild(el("text",{x:X0-10,y:232,"font-size":9.5,"font-family":"-apple-system,sans-serif","font-weight":700,"letter-spacing":"2",fill:"#9A8E7C"}, cfg.trendLabel));
    cfg.trend.forEach(function(pt,i){
      var tx = x(pt[0]);
      for(var v=0; v<pt[1]; v++){
        var cy = TREND_BASE - v*16;
        var isTop = v === pt[1]-1;
        svg.appendChild(el("circle",{cx:tx,cy:cy,r:4.5,fill:isTop?(i>=cfg.trendSplitIndex?"#7A3E22":"#B4633A"):"#EBDCC5"}));
      }
    });
    svg.appendChild(el("line",{x1:X0-10,y1:TREND_BASE+14,x2:X1+10,y2:TREND_BASE+14,stroke:"#EDE4D4","stroke-width":1}));
    svg.appendChild(el("text",{x:X0,y:TREND_BASE+30,"font-size":9.5,"font-family":"Menlo,monospace",fill:"#B8AC99"},"13:41"));
    svg.appendChild(el("text",{x:X1,y:TREND_BASE+30,"text-anchor":"end","font-size":9.5,"font-family":"Menlo,monospace",fill:"#B8AC99"},"14:45"));
    arcEl.appendChild(svg);
  }

  /* ---- boot: run every component's init in document order. Each init is a
   * no-op if its markup isn't present on the current screen. ---- */
  initNav();
  initExplainers();
  initTour();
  initStanceDials();
  initPhaseStrip();
  initArcTimeline();

  /* Expose the render fn + data for reuse/testing or a future React shim
   * (e.g. a thin wrapper that calls Qwendom.renderStanceDial-equivalent
   * logic inside a useEffect, or — better — a real <StanceDial> that
   * inlines this same SVG math as JSX). */
  Qwendom.renderStanceDial = renderStanceDial;
})();
