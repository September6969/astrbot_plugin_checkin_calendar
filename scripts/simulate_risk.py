"""复现风险曲线及固定随机种子的娱乐规则模拟，不读取真实用户记录。"""
import argparse,json,math,random,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT.parent))
from astrbot_plugin_checkin_calendar.store import blast_probability


def simulate(gap,runs=500,days=30):
    rng=random.Random(20261111+gap)
    successes=blasts=attempts=0
    for _ in range(runs):
        t=previous=short=long=0.0
        while t<days*24:
            short*=2**(-(t-previous)/6);long*=2**(-(t-previous)/36)
            previous=t;attempts+=1
            if rng.random()<blast_probability(short,long)/100:
                blasts+=1;t+=max(gap,12)
            else:
                successes+=1;short+=1;long+=1;t+=gap
    return {'interval_hours':gap,'attempts':attempts,'blast_fraction':blasts/attempts,'successful_per_day':successes/(runs*days)}


def main(output):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    output.mkdir(parents=True,exist_ok=True)
    attempts=[blast_probability(sum(2**(-2*j/6) for j in range(1,n)),sum(2**(-2*j/36) for j in range(1,n))) for n in range(1,13)]
    initial_short=sum(2**(-2*j/6) for j in range(6));initial_long=sum(2**(-2*j/36) for j in range(6))
    hours=[i/4 for i in range(289)]
    recovery=[blast_probability(initial_short*2**(-h/6),initial_long*2**(-h/36)) for h in hours]
    data={'conditional_all_previous_successes':attempts,'simulation':[simulate(g) for g in (2,4,8,12,24)],'recovery_hours':hours,'recovery_percent':recovery}
    (output/'simulation.json').write_text(json.dumps(data,indent=2),encoding='utf-8')
    fig,axes=plt.subplots(1,2,figsize=(11,4),layout='constrained')
    axes[0].plot(range(1,13),attempts,marker='o',color='#d46538');axes[0].set(title='Every 2 hours; previous attempts succeed',xlabel='Attempt number',ylabel='Blast probability (%)',ylim=(0,80))
    axes[1].plot(hours,recovery,color='#397db2');axes[1].axvline(12,color='#999999',linestyle='--',label='12 hours');axes[1].set(title='Rest after 6 successful records',xlabel='Rest (hours)',ylabel='Blast probability (%)',ylim=(0,80));axes[1].legend()
    for ax in axes: ax.grid(alpha=.2)
    fig.savefig(output/'risk-curves.png',dpi=160);plt.close(fig)
    print(json.dumps({'first_six':attempts[:6],'simulation':data['simulation']},indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True)
    main(parser.parse_args().output)
