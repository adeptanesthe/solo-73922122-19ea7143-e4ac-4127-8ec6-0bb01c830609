#!/usr/bin/env bash
# 口述史馆检索 API 演示脚本
# 场景：首查 → 撤稿 → 续页（跳过被撤版本、补足当页）→ 同号重刊
#      → 旧游标看不到重刊版 / 新检索能看到 → 撤稿不必等计分结束
#      → 搭配核查：改词距不重建索引；撤掉不含关键词的稿会改变榜序
# 用法：先启动服务（docker compose up --build 或本地 uvicorn），再执行 ./demo.sh
set -euo pipefail

BASE="${BASE_URL:-http://127.0.0.1:8000}"

post() { # path body
  curl -sS -X POST "$BASE$1" -H 'Content-Type: application/json' -d "$2"
}

publish() { # doc_id tokens-json
  post /manuscripts "$(jq -nc --arg d "$1" --argjson t "$2" '{doc_id:$d, tokens:$t}')"
}

hr() { echo; echo "================================================================"; }

echo "目标服务: $BASE"

# 本脚本按固定稿号与版本号断言，需要空库：已有数据时先 docker compose down -v
existing=$(curl -sS "$BASE/manuscripts" | jq '.manuscripts | length')
if [ "$existing" != "0" ]; then
  echo "✗ 库中已有 $existing 个稿件版本，演示需要在空库上运行。" >&2
  echo "  请先清空数据（docker compose down -v）或换一个 DB_PATH 重启服务。" >&2
  exit 1
fi

hr
echo "1) 编辑刊发 7 篇分词后的转写稿（稿号 MS-001..MS-007）"
publish MS-001 '["抗战","北平","胡同","童年","粮票"]'        | jq -c .
publish MS-002 '["抗战","上海","弄堂","工厂","夜校"]'        | jq -c .
publish MS-003 '["北平","茶馆","曲艺","抗战","胜利","广播"]' | jq -c .
publish MS-004 '["知青","下乡","北大荒","麦田"]'             | jq -c .
publish MS-005 '["抗战","西南联大","昆明","北平","师生"]'    | jq -c .
publish MS-006 '["北平","城墙","骆驼","运输队"]'             | jq -c .
publish MS-007 '["抗战","伤兵","医院","护士"]'               | jq -c .

hr
echo "2) 在刊稿号重复刊发 → 409"
code=$(curl -sS -o /tmp/dup.json -w '%{http_code}' -X POST "$BASE/manuscripts" \
  -H 'Content-Type: application/json' -d '{"doc_id":"MS-001","tokens":["抗战"]}')
echo "HTTP $code $(cat /tmp/dup.json)"

hr
echo "3) 首查：terms=[抗战, 北平]，页长 2（先看一下页长 100 的完整榜单作对照）"
post /search '{"terms":["抗战","北平"],"size":100}' | jq '{total_hits, hits:[.hits[]|{doc_id,version,score}]}'
echo "--- 页长 2 的首查 ---"
first=$(post /search '{"terms":["抗战","北平"],"size":2}')
echo "$first" | jq '{total_hits, hits, next_cursor}'
cursor=$(echo "$first" | jq -r .next_cursor)

hr
echo "4) 馆方撤掉 MS-003（原本第 3 名，将在第 2 页出现）"
post /manuscripts/MS-003/withdraw '' | jq -c .

hr
echo "5) 凭游标续页：MS-003 被跳过，MS-006/MS-007 从后续命中补足当页，分数与首查一致"
page2=$(post /search/next "$(jq -nc --arg c "$cursor" '{cursor:$c}')")
echo "$page2" | jq '{remaining_hits, hits, next_cursor}'
if echo "$page2" | jq -e '[.hits[].doc_id] | index("MS-003") | not' >/dev/null \
   && [ "$(echo "$page2" | jq -r '[.hits[].doc_id]|join(",")')" = "MS-006,MS-007" ]; then
  echo "✓ 已撤稿的 MS-003 未出现，当页由后续命中补足"
else
  echo "✗ 续页结果不符合预期"; exit 1
fi
cursor2=$(echo "$page2" | jq -r .next_cursor)

hr
echo "6) 同号重刊 MS-003（新内容、新版本号）"
post /manuscripts "$(jq -nc '{doc_id:"MS-003", tokens:["北平","抗战","重逢","口述","整理"]}')" | jq -c .

hr
echo "7) 旧游标继续翻页：重刊版本不进入首查前已开的检索"
page3=$(post /search/next "$(jq -nc --arg c "$cursor2" '{cursor:$c}')")
echo "$page3" | jq '{remaining_hits, hits, next_cursor}'
if echo "$page3" | jq -e '[.hits[].doc_id] | index("MS-003") | not' >/dev/null; then
  echo "✓ 重刊的 MS-003 未进入旧检索"
else
  echo "✗ 旧检索不应看到重刊版本"; exit 1
fi

hr
echo "8) 重刊后新开的检索：能看到 MS-003 新版本"
fresh=$(post /search '{"terms":["抗战","北平"],"size":100}')
echo "$fresh" | jq '[.hits[]|{doc_id,version,score}]'
if echo "$fresh" | jq -e '.hits[] | select(.doc_id=="MS-003" and .version==8)' >/dev/null; then
  echo "✓ 新检索命中重刊后的 MS-003 (version=8)"
else
  echo "✗ 新检索未命中重刊版本"; exit 1
fi

hr
echo "9) 撤稿不必等正在计分的请求结束"
echo "   后台发起一个人为放慢 4 秒的首查，期间立即撤掉 MS-002"
post /search '{"terms":["抗战"],"size":100,"debug_delay_seconds":4}' > /tmp/slow_search.json &
slow_pid=$!
sleep 0.5   # 确认对方进入计分阶段
t0=$(date +%s%3N)
curl -sS -X POST "$BASE/manuscripts/MS-002/withdraw" | jq -c .
t1=$(date +%s%3N)
echo "   撤稿耗时 $((t1 - t0)) ms（远小于计分的 4000 ms，未被阻塞）"
wait $slow_pid
echo "   慢检索仍按其首查时刻的快照返回（MS-002 在结果中）："
jq -c '[.hits[].doc_id]' /tmp/slow_search.json

hr
echo "10) 稿件版本终态"
curl -sS "$BASE/manuscripts" | jq '[.manuscripts[]|{version,doc_id,status}]'

hr
echo "11) 搭配核查：撤掉一篇不含关键词的在刊稿，榜序随之改变"
echo "   先用一组新稿做搭配演示（关键词：矿井）"
publish CO-001 '["矿井","巷道","工牌","老周"]'      | jq -c '{doc_id,version}'
publish CO-002 '["早班","矿井","巷道","灯房"]'      | jq -c '{doc_id,version}'
publish CO-003 '["矿井","风机","巷道","煤车"]'      | jq -c '{doc_id,version}'
publish CO-004 '["班长","矿井","风机","煤车"]'      | jq -c '{doc_id,version}'
publish CO-005 '["巷道","风机","井口"]'             | jq -c '{doc_id,version}'
publish CO-006 '["巷道","学徒","夜校"]'             | jq -c '{doc_id,version}'
echo "--- 词距 2 的搭配榜（只列分数/同现稿数/最近出处）---"
colloc() { post /collocations "$1"; }
before=$(colloc '{"term":"矿井","max_distance":2,"limit":20}')
echo "$before" | jq '{published_manuscripts, keyword_doc_count,
  results:[.results[]|{collocate,score,cooccurring_docs,evidence}]}'
echo "--- 换成词距 1：无需重建索引，同现稿数立即变化 ---"
colloc '{"term":"矿井","max_distance":1,"limit":20}' \
  | jq '{max_distance, results:[.results[]|{collocate,cooccurring_docs}]}'

echo "--- 撤掉 CO-006（不含关键词“矿井”，只改变总稿数与“巷道”的在刊稿数）---"
post /manuscripts/CO-006/withdraw '' | jq -c .
after=$(colloc '{"term":"矿井","max_distance":2,"limit":20}')
echo "$after" | jq '{published_manuscripts, keyword_doc_count,
  results:[.results[]|{collocate,score,cooccurring_docs,evidence}]}'
if [ "$(echo "$before" | jq -r '[.results[].collocate]|join(",")')" = "风机,巷道" ] \
   && [ "$(echo "$after" | jq -r '[.results[].collocate]|join(",")')" = "巷道,风机" ]; then
  b_fan=$(echo "$before" | jq -r '.results[]|select(.collocate=="风机").score')
  b_alley=$(echo "$before" | jq -r '.results[]|select(.collocate=="巷道").score')
  a_fan=$(echo "$after" | jq -r '.results[]|select(.collocate=="风机").score')
  a_alley=$(echo "$after" | jq -r '.results[]|select(.collocate=="巷道").score')
  echo "✓ 撤掉不含关键词的 CO-006 后榜序改变："
  echo "  撤前 风机 $b_fan > 巷道 $b_alley ；撤后 巷道 $a_alley > 风机 $a_fan"
  echo "  （同现稿数不变，总稿数与“巷道”在刊稿数变化使分数重排）"
else
  echo "✗ 搭配榜序未按预期变化"; exit 1
fi

hr
echo "12) 核查期间可刊撤：慢速核查读的是同一份已提交语料"
echo "   后台发起延迟 3 秒的核查，期间撤掉不含关键词的 CO-005"
post /collocations '{"term":"矿井","max_distance":2,"limit":20,"debug_delay_seconds":3}' \
  > /tmp/slow_colloc.json &
slow_pid=$!
sleep 0.5
t0=$(date +%s%3N)
curl -sS -X POST "$BASE/manuscripts/CO-005/withdraw" | jq -c .
t1=$(date +%s%3N)
echo "   撤稿耗时 $((t1 - t0)) ms（远小于核查的 3000 ms，未被阻塞）"
wait $slow_pid
snapshot_n=$(jq -r .published_manuscripts /tmp/slow_colloc.json)
echo "   慢核查仍按撤稿前的快照返回（published_manuscripts=$snapshot_n，撤稿前语料）："
jq -c '{published_manuscripts, results:[.results[]|{collocate,cooccurring_docs}]}' \
  /tmp/slow_colloc.json

hr
echo "13) 索引导航校验（升级/撤稿后统计与在刊稿一致）"
curl -sS "$BASE/collocations/upgrade-check" | jq -c .
echo
echo "演示完成。"
