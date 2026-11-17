import { it,expect } from 'vitest';
import { mkdtempSync,rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { recordHumanInput, readHumanInput, restoreHumanInputBoundary,
  latestHumanInput, invalidateHumanInput } from '../src/scl-human-decision.js';

it('records and reads an authenticated human input from Host storage', () => {
  const root=mkdtempSync(join(tmpdir(),'scl-input-test-'));
  try {
    const env={SCL_HUMAN_DECISION_KEY_FILE:join(root,'key')};
    const source={channel:'telegram' as const,eventId:'event-17',humanRef:'human',message:'I already authorized this repair. Resume the original task.'};
    const ref=recordHumanInput(source,'human:reader',env);
    expect(recordHumanInput(source,'human:reader',env)).toBe(ref);
    const observed=readHumanInput(ref,'human:reader','telegram','human',env);
    expect(Number.isSafeInteger(observed.observedAtMs)).toBe(true);
    expect(()=>readHumanInput(ref,'human:other','telegram','human',env)).toThrow('another conversation');
    expect(()=>readHumanInput(ref,'human:reader','feishu','human',env)).toThrow('another conversation');
    expect(()=>readHumanInput('0'.repeat(64),'human:reader','telegram','human',env)).toThrow();
    expect(()=>recordHumanInput({...source,message:'invented authorization'},'human:reader',env)).toThrow('conflicts');
  } finally {rmSync(root,{recursive:true,force:true});}
});

it('restores the original durable boundary without changing provenance or overriding newer live input',()=>{
  const root=mkdtempSync(join(tmpdir(),'scl-boundary-test-'));
  try{
    const env={SCL_HUMAN_DECISION_KEY_FILE:join(root,'key')};
    const source={channel:'feishu' as const,eventId:'original-event',humanRef:'human',message:'recover'};
    const ref=recordHumanInput(source,'human:pulse',env);
    const before=readHumanInput(ref,'human:pulse','feishu','human',env);
    invalidateHumanInput('human:pulse',env);
    expect(restoreHumanInputBoundary(ref,'human:pulse','feishu','human',env)).toEqual(before);
    expect(latestHumanInput('human:pulse',env)).toBe(ref);
    const newer=recordHumanInput({...source,eventId:'later-event',message:'stop'},'human:pulse',env);
    expect(()=>restoreHumanInputBoundary(ref,'human:pulse','feishu','human',env)).toThrow('supersedes');
    expect(latestHumanInput('human:pulse',env)).toBe(newer);
    expect(readHumanInput(ref,'human:pulse','feishu','human',env)).toEqual(before);
  }finally{rmSync(root,{recursive:true,force:true});}
});

it('binds provenance to each dispatched object, not the latest input or a copied prompt', async () => {
  const {annotatedHumanMessage} = await import('../src/scl-human-decision.js');
  const {humanMessageProof} = await import('../src/collaboration-capture.js');
  const root=mkdtempSync(join(tmpdir(),'scl-input-proof-'));
  try {
    const env={SCL_HUMAN_DECISION_KEY_FILE:join(root,'key')};
    const source={channel:'telegram' as const,eventId:'input-A',humanRef:'human',message:'original-A'};
    const first=annotatedHumanMessage('enriched-A',source,'human:pulse',env);
    const second=annotatedHumanMessage('enriched-B',{...source,eventId:'input-B',message:'original-B'},'human:pulse',env);
    const replay=annotatedHumanMessage('different-enrichment',source,'human:pulse',env);
    expect(humanMessageProof(first)).toEqual(humanMessageProof(replay));
    expect(humanMessageProof(first)?.ref).not.toBe(humanMessageProof(second)?.ref);
    expect(humanMessageProof({...first})).toBeUndefined();
    expect(humanMessageProof({type:'human',content:first.content})).toBeUndefined();
    expect(JSON.stringify(first)).not.toContain('observedAtMs');
    expect(Object.isFrozen(humanMessageProof(first))).toBe(true);
  } finally {rmSync(root,{recursive:true,force:true});}
});

it('closes a media message with the person\'s words and their provenance', async () => {
  const { annotatedMediaMessage } = await import('../src/scl-human-decision.js');
  const root=mkdtempSync(join(tmpdir(),'scl-media-test-'));
  try {
    const env={SCL_HUMAN_DECISION_KEY_FILE:join(root,'key')};
    const source={channel:'telegram' as const,eventId:'photo-1',humanRef:'human',message:'这个要改'};
    const image={type:'image',source:{type:'base64',media_type:'image/png',data:'AA=='}};
    const photo=annotatedMediaMessage({type:'human',content:[image]},'这个要改',source,'human:ken',env);
    const blocks=photo.content as Array<{type:string;text?:string}>;
    expect(blocks[0]).toEqual(image);
    expect(blocks[1].text).toMatch(/^这个要改\n\n\[Host human input reference: [a-f0-9]{64}\. /);
    const ref=blocks[1].text!.match(/[a-f0-9]{64}/)![0];
    expect(readHumanInput(ref,'human:ken','telegram','human',env).message).toBe('这个要改');
    const file=annotatedMediaMessage({type:'human',content:[{type:'text',text:'[文件: a.txt]\nx'}]},'[文件: a.txt]',
      {...source,eventId:'file-1',message:'[文件: a.txt]'},'human:ken',env);
    expect((file.content as Array<{text?:string}>)[1].text).toMatch(/^\n\n\[文件: a\.txt\]\n\n\[Host human input reference: /);
    const voice=annotatedMediaMessage({type:'human',content:'语音转写'},'语音转写',{...source,eventId:'voice-1',message:'语音转写'},'human:ken',env);
    expect(voice.content).toMatch(/^语音转写\n\n\[Host human input reference: /);
    expect(annotatedMediaMessage({type:'human',content:[image]},'',undefined,'human:ken',env).content).toEqual([image]);
  } finally {rmSync(root,{recursive:true,force:true});}
});


it('preserves raw media provenance when display text expands a hidden link, including replay', async () => {
  const { annotatedMediaMessage } = await import('../src/scl-human-decision.js');
  const root = mkdtempSync(join(tmpdir(), 'scl-media-replay-'));
  try {
    const env = { SCL_HUMAN_DECISION_KEY_FILE: join(root, 'key') };
    const source = { channel: 'telegram' as const, eventId: 'photo-hidden-link', humanRef: 'human', message: '看原文' };
    const original = annotatedMediaMessage({ type: 'human', content: [] }, '看原文', source, 'human:ken', env);
    const replay = annotatedMediaMessage({ type: 'human', content: [] }, '看原文 (https://example.org/target)', source, 'human:ken', env);
    expect(JSON.stringify(replay.content)).not.toContain('DEGRADED');
    const ref = JSON.stringify(original.content).match(/[a-f0-9]{64}/)![0];
    expect(readHumanInput(ref, 'human:ken', 'telegram', 'human', env).message).toBe('看原文');
  } finally { rmSync(root, { recursive: true, force: true }); }
});

it('states message provenance and nothing more', async () => {
  const { humanInputContext } = await import('../src/scl-human-decision.js');
  const ref='a'.repeat(64);
  expect(humanInputContext(ref)).toBe(`\n\n[Host human input reference: ${ref}. This records message provenance.]`);
  expect(humanInputContext(ref)).not.toMatch(/authori[sz]/i);
  // Readers match the prefix, so earlier annotations stay readable.
  const prefix=new RegExp(`\\[Host human input reference: ${ref}\\.`);
  expect(humanInputContext(ref)).toMatch(prefix);
  expect(`[Host human input reference: ${ref}. This records message provenance, not blanket authorization.]`).toMatch(prefix);
});
