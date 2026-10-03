import argparse
import getpass
import json
import pandas as pd
from . import db
from .security import set_secret
from .data import generate_demo,register_dataset


def main():
    p=argparse.ArgumentParser(description="因子研究室：本机配置工具")
    sub=p.add_subparsers(dest="command",required=True)
    secret=sub.add_parser("set-secret",help="通过系统凭据管理器保存密钥，输入不会显示")
    secret.add_argument("name",nargs='?')
    sub.add_parser("demo",help="生成明确标识的合成演示数据")
    imp=sub.add_parser("import-data",help="导入标准化的财报和行情 Parquet 文件")
    imp.add_argument("dataset_id");imp.add_argument("financials");imp.add_argument("market")
    imp.add_argument("--source",required=True);imp.add_argument("--pit-verified",action="store_true")
    args=p.parse_args()
    db.initialize()
    if args.command=="set-secret":
        name=args.name or input('凭据名称（例如 tavily / ricequant / research-model）：').strip()
        value=getpass.getpass("输入密钥（不会显示，也不会写入日志）：")
        if not value: raise ValueError("密钥不能为空")
        set_secret(name,value)
        print("已保存到系统凭据管理器。网页只会显示连接是否已配置。")
    elif args.command=="demo": print(json.dumps(generate_demo(),ensure_ascii=False,indent=2))
    else: print(json.dumps(register_dataset(args.dataset_id,pd.read_parquet(args.financials),pd.read_parquet(args.market),args.source,args.pit_verified),ensure_ascii=False,indent=2))


if __name__=="__main__":main()
