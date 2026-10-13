"""兼容入口：实际实现见 judge_agent_main.py。"""

from agents.judge_agent.judge_agent_main import build_arg_parser, run_pipeline, _resolve_input_dir


if __name__ == "__main__":
    print("========== judge_agent 启动（当前实现：第四部分） ==========")
    args = build_arg_parser().parse_args()

    input_dir = _resolve_input_dir(args.input_dir)
    if not input_dir:
        print("您已取消选择。程序退出。")
        raise SystemExit(0)

    json_file, md_file = run_pipeline(
        input_dir=input_dir,
        output_dir=args.output_dir,
        report_date=args.report_date,
        subject_name_hint=args.subject_name_hint,
        chat_model=args.chat_model,
        timeout_seconds=max(30, args.timeout),
        max_attempts=max(1, args.max_attempts),
        disable_llm=args.disable_llm,
        visual_json_path=args.visual_json,
        llm_provider=args.llm_provider,
        local_llm_base_url=args.local_llm_base_url,
        local_chat_model=args.local_chat_model,
        cloud_chat_model=args.cloud_chat_model,
        allow_cloud=args.allow_cloud,
    )

    print(f"[*] JSON输出: {json_file}")
    print(f"[*] Markdown输出: {md_file}")
