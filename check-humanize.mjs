// Verify the actual unmodified GitHub module, with no network, model key or real messages.
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import fs from 'node:fs';
import { Humanizer } from './vendor/ai-companion/humanize.js';

const metadata=JSON.parse(fs.readFileSync(new URL('./vendor/ai-companion/UPSTREAM.json',import.meta.url)));
const source=fs.readFileSync(new URL('./vendor/ai-companion/humanize.js',import.meta.url));
assert.equal(createHash('sha256').update(source).digest('hex'),metadata.sha256);
const human=new Humanizer({jitter:0,longGapChance:0});
const night=new Date().getHours()>=2&&new Date().getHours()<7;
assert.equal(human.typingMsFor('海'),Math.round((1200+85)*(night?1.6:1)));
assert.equal(human.typingMsFor('海'.repeat(100)),Math.round(6000*(night?1.6:1)));
const parts=['记住啦。','给你留一片海。'],sent=[],waits=[],typing=[];
await human.say(parts,{send:async line=>sent.push(line),sleep:async ms=>waits.push(ms),setTyping:async on=>typing.push(on)});
assert.deepEqual(sent,parts);
assert.deepEqual(typing,[true,false,true,false]);
assert.equal(waits.length,3);
assert.ok(waits[1]>=700&&waits[1]<=2600);
let attempts=0;
await assert.rejects(human.say(parts,{send:async()=>{attempts++;throw Error('Delivery unknown');},sleep:async()=>{}}));
assert.equal(attempts,1);
assert.deepEqual(human.split(''),[]);
console.log('PASS: pinned upstream Humanizer, Unicode length, night slowdown, ordered parts, random gap and no resend after failure');
