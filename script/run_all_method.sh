cd .. && ls

for j in 1 2 3
do
    for i in 'fedavg' 'fedrs' 'fedrod' 'moon' 'fedbalance' 'fedprox'
    do
        python main.py --method $i  --thread_number 5 --dataset cifar100 --client_number 100  --comm_round 500 --partition_alpha 0.5
    done
done
