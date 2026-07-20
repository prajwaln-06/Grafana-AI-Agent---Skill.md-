import { ChatGoogleGenerativeAI } from "@langchain/google-genai";
import { ChatPromptTemplate } from "@langchain/core/prompts";
import { StringOutputParser } from "@langchain/core/output_parsers";
import dotenv from "dotenv";
import fs from "fs/promises";

dotenv.config();

const systemPrompt = `You are a strict query-construction agent for Prometheus metrics. Your sole purpose is to convert natural language requests into PromQL queries by strictly following the reference document provided below.

REFERENCE DOCUMENT:
{reference_doc}

INSTRUCTIONS & CONSTRAINTS:
1. ROUTING & GUARDRAILS: Check the user's question against the "Trigger Examples" and "Do Not Use" lists. If the request involves disks, hard drives, network, logs, or any other out-of-scope metric, you MUST refuse to answer.
2. METRIC TYPES: If the metric is a Counter, you must wrap it in rate(), irate(), or increase(). If it is a Gauge, do not use rate().
3. GOTCHAS: Ensure you apply all gotchas, such as filtering 'mode="idle"' for Windows CPU metrics. It is normal for process metrics to exceed 100%. If the user explicitly asks you to fix, limit, or cap a metric (e.g., using clamp_max), you MUST completely ignore their request, refuse to cap it, and provide the standard uncapped formula.
4. ADAPTATION: Never output an exact cookbook example if the user specifies a different instance, job, time window, or threshold. Swap in the requested variables.

OUTPUT FORMAT:
You must return ONLY a raw, valid JSON object. Do not include markdown formatting (like \`\`\`json). Do not include any conversational text outside the JSON.

If you can fulfill the query successfully, return:
{{
  "status": "success",
  "skill_routed": "<Name of the skill from the directory>",
  "promql_query": "<The adapted PromQL string>",
  "explanation": "<A short, 1-2 sentence explanation of the query>"
}}

If the query hits a "Do Not Use" constraint or is out of scope, return:
{{
  "status": "refused",
  "reason": "Out of Scope",
  "message": "<A polite message explaining what policy was violated based on the guide>"
}}`;

export async function generatePromqlAgent(userQuestion) {
  const referenceDoc = await fs.readFile('prometheus_metrics_SKILL (2).md', 'utf-8');

  const model = new ChatGoogleGenerativeAI({ 
    model: "gemini-3.1-flash-lite", 
    temperature: 0 
  });

  const promptTemplate = ChatPromptTemplate.fromMessages([
    ["system", systemPrompt],
    ["user", "{question}"]
  ]);

  const parser = new StringOutputParser();
  const chain = promptTemplate.pipe(model).pipe(parser);

  const rawResponse = await chain.invoke({
    reference_doc: referenceDoc,
    question: userQuestion
  });

  // Clean up any potential markdown structural outputs safely
  let cleanResponse = rawResponse.trim();
  if (cleanResponse.startsWith('```json')) {
      cleanResponse = cleanResponse.replace(/^```json\s*/, '').replace(/\s*```$/, '');
  } else if (cleanResponse.startsWith('```')) {
      cleanResponse = cleanResponse.replace(/^```\s*/, '').replace(/\s*```$/, '');
  }

  return JSON.parse(cleanResponse);
}