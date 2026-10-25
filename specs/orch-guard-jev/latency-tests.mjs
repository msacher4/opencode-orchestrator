import { G, entries } from "./banc-core.mjs";

let testCmd=null, testAgent=null;
const seen=new Set();
for(const e of entries){
  if(e.agent!=="orchestrator"&&e.agent!=="reviewer")continue;
  if(seen.has(e.cmd))continue; seen.add(e.cmd);
  if(G.jevLikelySecret(e.cmd))continue;
  if(G.jevIsArbitraryExecutionWrapper(e.cmd))continue;
  const r=G.check({agent:e.agent,tool:"shell",input:{command:e.cmd},sessionID:"bench-t3"});
  if(G.jevIsUnknownWordRefusal(e.agent,r.blocked)){testCmd=e.cmd;testAgent=e.agent;break;}
}
const det=G.check({agent:testAgent,tool:"shell",input:{command:testCmd},sessionID:"bench-t3"});
const input={agent:testAgent,tool:"shell",input:{command:testCmd},sessionID:"bench-t3"};

const origFetch=globalThis.fetch;
const origKey=G.jevReadApiKey;
G.jevReadApiKey=()=>"mock-key";
const mockResponse={ok:true,status:200,json:async()=>({answers:{classification:{choice:"lecture",probabilities:{lecture:0.99,mutation:0.01},confidence:0.99}},model:"jev-latest",usage:{}})};
globalThis.fetch=async()=>new Promise(res=>setTimeout(res,2)).then(()=>mockResponse);

const cacheLats=[];
for(let i=0;i<50;i++){
  const t0=Date.now();
  await G.jevSecondOpinion(input,det);
  cacheLats.push(Date.now()-t0);
}
cacheLats.sort((a,b)=>a-b);
console.log("CACHE (50 appels, ms) p50:",cacheLats[Math.floor(cacheLats.length*0.5)],"p95:",cacheLats[Math.ceil(cacheLats.length*0.95)-1],"min:",cacheLats[0],"max:",cacheLats[cacheLats.length-1]);

const mockLats=[];
for(let i=0;i<50;i++){
  const t0=Date.now();
  await G.jevSecondOpinion(input,det);
  mockLats.push(Date.now()-t0);
}
mockLats.sort((a,b)=>a-b);
console.log("MOCK (50 appels, ms) p50:",mockLats[Math.floor(mockLats.length*0.5)],"p95:",mockLats[Math.ceil(mockLats.length*0.95)-1],"min:",mockLats[0],"max:",mockLats[mockLats.length-1]);

globalThis.fetch=origFetch; G.jevReadApiKey=origKey;
console.log("LATENCY_TESTS_DONE");
