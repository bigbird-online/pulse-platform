/** CC exposes the same native core tool. No retired lifecycle implementation. */
import { Server } from '@modelcontextprotocol/sdk/server/index.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import { CallToolRequestSchema, ListToolsRequestSchema } from '@modelcontextprotocol/sdk/types.js';
import {randomUUID} from 'node:crypto';
import {installPiWorkItemCore,invokeOperation} from './pi-work-item-core.mjs';
import {requestCustody} from './core-request-custody.mjs';
import {STATS_TOOL_NAME,statsAvailable,statsDescription,statsParameters,runStatsQuery} from './pulse-stats-tool.mjs';
import {JOBS_TOOL_NAME,jobsDescription,jobsParameters,runJobsTool} from './pulse-jobs-tool.mjs';
const invocation=randomUUID();
const host=JSON.parse(process.env.PULSE_WORK_ITEM_CORE_HOST ?? '{}');
function tool(custody) {
  let definition;
  installPiWorkItemCore({on(){},registerTool(value){definition=value;},appendEntry:custody?.appendEntry},host);
  return definition;
}
const definition=tool();
const server=new Server({name:'pulse-native-core',version:'1.0.0'},{capabilities:{tools:{}}});
// Read-only Stats query; listed only where the Stats release that answers it is installed.
const stats=statsAvailable();
server.setRequestHandler(ListToolsRequestSchema,async()=>({tools:[{name:definition.name,
  description:definition.description+' '+definition.promptGuidelines.join(' '),inputSchema:definition.parameters},
  ...(stats?[{name:STATS_TOOL_NAME,description:statsDescription,inputSchema:statsParameters}]:[]),
  {name:JOBS_TOOL_NAME,description:jobsDescription,inputSchema:jobsParameters}]}));
server.setRequestHandler(CallToolRequestSchema,async({params},extra)=>{
  if(stats && params.name===STATS_TOOL_NAME) {
    try {
      const {isError,text}=await runStatsQuery(params.arguments ?? {});
      return {isError,content:[{type:'text',text}]};
    } catch(error) {return {isError:true,content:[{type:'text',text:String(error.message||error)}]};}
  }
  if(params.name===JOBS_TOOL_NAME) {
    try {
      const {isError,text}=await runJobsTool(params.arguments ?? {},()=>invokeOperation(host,{action:'identity'}));
      return {isError,content:[{type:'text',text}]};
    } catch(error) {return {isError:true,content:[{type:'text',text:String(error.message||error)}]};}
  }
  if(params.name!==definition.name) throw new Error('Unknown native WorkItem tool');
  try {
    const identity=await invokeOperation(host,{action:'identity'});
    if(identity.schema!=='harness.native_identity/v1' || !identity.principal?.session)
      return {isError:true,content:[{type:'text',text:JSON.stringify(identity)}]};
    const custody=requestCustody(process.env.PULSE_CORE_REQUEST_STATE_ROOT,JSON.stringify(identity.principal));
    return await tool(custody).execute(invocation+':'+String(extra.requestId),params.arguments,undefined,undefined,
      {sessionManager:custody});
  } catch(error) {return {isError:true,content:[{type:'text',text:String(error.message||error)}]};}
});
await server.connect(new StdioServerTransport());
